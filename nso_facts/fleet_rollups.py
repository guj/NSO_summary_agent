"""Pure fleet rollup math over topology / sync / health facts (no report text)."""

from __future__ import annotations

import re
from typing import Any

from nso_facts.fleet_summary_thresholds import (
    FleetSummaryThresholds,
    load_fleet_summary_thresholds,
)

_OK = frozenset({"up"})

_UNMAPPED_ISIS = re.compile(
    r"^(?P<device>\S+)\s+\S+:\s+unmapped IS-IS system id ['\"]?(?P<label>[^'\"]+)['\"]?\s*$"
)
_UNMAPPED_BGP = re.compile(
    r"^(?P<device>\S+)\s+(?P<label>\S+):\s+could not map neighbor address"
)


def _intish(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def operational_layers(topology: Any) -> dict[str, Any] | None:
    if not isinstance(topology, dict):
        return None
    operational = topology.get("operational")
    if not isinstance(operational, dict):
        return None
    layers = operational.get("layers")
    return layers if isinstance(layers, dict) else None


def static_layers(topology: Any) -> dict[str, Any]:
    if not isinstance(topology, dict):
        return {}
    static = topology.get("static")
    if not isinstance(static, dict):
        return {}
    layers = static.get("layers")
    return layers if isinstance(layers, dict) else {}


def operational_issues(topology: Any) -> list[dict[str, Any]]:
    if not isinstance(topology, dict):
        return []
    operational = topology.get("operational")
    if not isinstance(operational, dict):
        return []
    issues = operational.get("issues") or []
    return [i for i in issues if isinstance(i, dict)]


def merged_edges(
    op_layers: dict[str, Any],
    static_layers_map: dict[str, Any],
    layer: str,
) -> list[dict[str, Any]]:
    op_edges = (op_layers.get(layer) or {}).get("edges") or []
    static_edges = (static_layers_map.get(layer) or {}).get("edges") or []
    by_id = {
        edge["id"]: edge
        for edge in static_edges
        if isinstance(edge, dict) and isinstance(edge.get("id"), str)
    }
    merged: list[dict[str, Any]] = []
    for edge in op_edges:
        if not isinstance(edge, dict):
            continue
        static = by_id.get(edge.get("id")) if isinstance(edge.get("id"), str) else None
        if static is None:
            if (edge.get("local") or {}).get("device") or (
                edge.get("remote") or {}
            ).get("device"):
                merged.append(edge)
            continue
        row = dict(edge)
        if "local" not in row or row.get("local") is None:
            row["local"] = static.get("local")
        if "remote" not in row or row.get("remote") is None:
            row["remote"] = static.get("remote")
        merged.append(row)
    return merged


def devices_in_edges(*edge_lists: list) -> set[str]:
    names: set[str] = set()
    for edges in edge_lists:
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            for end in ("local", "remote"):
                device = (edge.get(end) or {}).get("device")
                if isinstance(device, str) and device.strip():
                    names.add(device.strip())
    return names


def edge_status(edge: dict[str, Any]) -> str:
    status = (edge.get("state") or {}).get("status", "unknown")
    return str(status).lower() if status is not None else "unknown"


def peer_up_total(device: str, edges: list) -> tuple[int, int]:
    mine = [
        e
        for e in edges
        if isinstance(e, dict)
        and device
        in {
            (e.get("local") or {}).get("device"),
            (e.get("remote") or {}).get("device"),
        }
    ]
    up = sum(1 for e in mine if edge_status(e) in _OK)
    return up, len(mine)


def route_summaries(topology: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(topology, dict):
        return {}
    operational = topology.get("operational")
    if not isinstance(operational, dict):
        return {}
    summary = operational.get("route_summary")
    if not isinstance(summary, dict):
        return {}
    return {
        str(device): entry
        for device, entry in summary.items()
        if isinstance(entry, dict)
    }


def unmapped_by_device(
    topology: Any,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return (bgp_unmapped, isis_unmapped) keyed by local device name."""
    bgp: dict[str, list[str]] = {}
    isis: dict[str, list[str]] = {}
    for issue in operational_issues(topology):
        code = issue.get("code")
        message = str(issue.get("message") or "")
        if code == "unknown_neighbor_address":
            match = _UNMAPPED_BGP.match(message)
            if not match:
                continue
            device = match.group("device")
            label = match.group("label")
            bgp.setdefault(device, [])
            if label not in bgp[device]:
                bgp[device].append(label)
        elif code == "unknown_neighbor_system_id":
            match = _UNMAPPED_ISIS.match(message)
            if not match:
                continue
            device = match.group("device")
            label = match.group("label")
            isis.setdefault(device, [])
            if label not in isis[device]:
                isis[device].append(label)
    return bgp, isis


def devices_in_topology(topology: Any) -> set[str]:
    """Device set used for fleet totals when sync summary is unavailable."""
    op_layers = operational_layers(topology)
    if op_layers is None:
        return set()
    static = static_layers(topology)
    phys = merged_edges(op_layers, static, "physical")
    under = merged_edges(op_layers, static, "underlay")
    route = merged_edges(op_layers, static, "routing")
    unmapped_bgp, unmapped_isis = unmapped_by_device(topology)
    return (
        devices_in_edges(phys, under, route)
        | set(unmapped_bgp)
        | set(unmapped_isis)
        | set(route_summaries(topology))
    )


def fleet_device_sync_counts(
    fleet_sync: Any,
    topology: Any,
) -> tuple[str | int, str | int, str | int]:
    """Return (total, in_sync, out_of_sync) — ints or ``—`` when sync unknown.

    ``out_of_sync`` here includes sync ``error`` devices (Fleet Summary style).
    """
    total, in_sync, out_of_sync, error = fleet_device_sync_breakdown(
        fleet_sync, topology
    )
    if in_sync == "—":
        return total, in_sync, "—"
    return total, in_sync, int(out_of_sync) + int(error)


def fleet_device_sync_breakdown(
    fleet_sync: Any,
    topology: Any,
) -> tuple[str | int, str | int, int, int]:
    """Return (total, in_sync, out_of_sync, error).

    When sync is unavailable: total from topology (or 0), in_sync is ``—``,
    out_of_sync and error are 0.
    """
    if isinstance(fleet_sync, dict) and fleet_sync.get("status") == "success":
        data = fleet_sync.get("data") or {}
        summary = data.get("summary") or {}
        devices = data.get("devices") or []
        in_sync = _intish(summary.get("in_sync"))
        out_of_sync = _intish(summary.get("out_of_sync"))
        error = _intish(summary.get("error"))
        if isinstance(devices, list) and devices:
            total = len(devices)
        else:
            total = in_sync + out_of_sync + error
        return total, in_sync, out_of_sync, error

    total = len(devices_in_topology(topology))
    return total, "—", 0, 0


def mismatch_by_edge_id(topology: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for issue in operational_issues(topology):
        if issue.get("code") != "config_live_mismatch":
            continue
        edge_id = issue.get("edge_id")
        if isinstance(edge_id, str):
            out[edge_id] = issue
    return out


def _admin_oper(edge: dict[str, Any]) -> tuple[str, str]:
    state = edge.get("state") if isinstance(edge.get("state"), dict) else {}
    admin = str(state.get("admin_status") or state.get("admin") or "").lower()
    oper = str(state.get("oper_status") or state.get("oper") or "").lower()
    if admin in {"up", "down", "admin-down", "unknown"} and oper in {
        "up",
        "down",
        "admin-down",
        "unknown",
    }:
        return admin or "unknown", oper or "unknown"
    st = edge_status(edge)
    if st == "up":
        return "up", "up"
    if st == "admin-down":
        return "admin-down", "admin-down"
    if st == "degraded":
        return "up", "up"
    if st == "unknown":
        return "unknown", "unknown"
    return "down", "down"


def iface_pair_status(edge: dict[str, Any]) -> str:
    admin, oper = _admin_oper(edge)
    if admin == "admin-down" or oper == "admin-down":
        return "admin-down"
    if admin == "unknown" or oper == "unknown":
        return "unknown"
    return f"{admin}/{oper}"


def device_needs_inventory_review(
    device: str,
    phys: list[dict[str, Any]],
    mismatch_by_edge: dict[str, dict[str, Any]],
) -> bool:
    for e in phys:
        if not isinstance(e, dict):
            continue
        if (e.get("local") or {}).get("device") != device:
            continue
        pair = iface_pair_status(e)
        if pair in {"up/down", "unknown"}:
            return True
        edge_id = str(e.get("id") or "")
        issue = mismatch_by_edge.get(edge_id)
        if issue and not issue.get("confirmed"):
            return True
    for issue in mismatch_by_edge.values():
        if issue.get("confirmed"):
            continue
        if issue.get("device") == device:
            return True
    return False


def fleet_inventory_review_count(topology: Any, fleet_sync: Any = None) -> int:
    """Count devices whose Interfaces column would be Inventory Review."""
    del fleet_sync  # reserved for parity with report helper signature
    op_layers = operational_layers(topology)
    if op_layers is None:
        return 0
    static = static_layers(topology)
    phys = merged_edges(op_layers, static, "physical")
    under = merged_edges(op_layers, static, "underlay")
    route = merged_edges(op_layers, static, "routing")
    unmapped_bgp, unmapped_isis = unmapped_by_device(topology)
    devices = sorted(
        devices_in_edges(phys, under, route)
        | set(unmapped_bgp)
        | set(unmapped_isis)
        | set(route_summaries(topology))
    )
    mismatch = mismatch_by_edge_id(topology)
    return sum(
        1 for d in devices if device_needs_inventory_review(d, phys, mismatch)
    )

def fleet_service_instance_counts(counts: dict[str, Any]) -> tuple[int, int]:
    operational = 0
    degraded = 0
    for bucket in (counts or {}).values():
        if not isinstance(bucket, dict):
            continue
        operational += _intish(bucket.get("up"))
        degraded += (
            _intish(bucket.get("down"))
            + _intish(bucket.get("degraded"))
            + _intish(bucket.get("unknown"))
        )
    return operational, degraded


def fleet_routing_totals(topology: Any) -> tuple[int, int, int, int]:
    """Sum per-device BGP/IS-IS up/total (matches Device Health column sums)."""
    op_layers = operational_layers(topology)
    if op_layers is None:
        return 0, 0, 0, 0
    static = static_layers(topology)
    under = merged_edges(op_layers, static, "underlay")
    route = merged_edges(op_layers, static, "routing")
    devices = sorted(devices_in_edges([], under, route))
    bgp_up = bgp_total = isis_up = isis_total = 0
    for device in devices:
        u, t = peer_up_total(device, route)
        bgp_up += u
        bgp_total += t
        u, t = peer_up_total(device, under)
        isis_up += u
        isis_total += t
    return bgp_up, bgp_total, isis_up, isis_total


def fleet_infra_alert_counts(
    system_health: Any,
    thresholds: FleetSummaryThresholds | None = None,
) -> tuple[int, int]:
    thr = thresholds or load_fleet_summary_thresholds()
    if not isinstance(system_health, dict):
        return 0, 0
    cpu_alerts = 0
    mem_alerts = 0
    for entry in system_health.values():
        if not isinstance(entry, dict):
            continue
        cpu = entry.get("cpu")
        if isinstance(cpu, dict) and "five_min" in cpu:
            if _intish(cpu.get("five_min")) >= thr.cpu_five_min_pct:
                cpu_alerts += 1
        memory = entry.get("memory")
        if isinstance(memory, dict) and memory.get("used_pct") is not None:
            try:
                used = float(memory.get("used_pct"))
            except (TypeError, ValueError):
                used = None
            if used is not None and used >= thr.memory_used_pct:
                mem_alerts += 1
    return cpu_alerts, mem_alerts
