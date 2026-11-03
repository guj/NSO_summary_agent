"""Per-device health labels and rows for the operator report and metrics."""

from __future__ import annotations

from typing import Any

from nso_facts.health import build_device_sync_map
from nso_facts.fleet_rollups import (
    devices_in_edges,
    merged_edges,
    mismatch_by_edge_id,
    operational_layers,
    peer_up_total,
    static_layers,
    unmapped_by_device,
)
from nso_report.devices import (
    INVENTORY_REVIEW_STATUS,
    _iface_pair_status,
)


def device_interfaces_label(
    device: str,
    phys: list,
    mismatch_by_edge: dict[str, dict[str, Any]],
) -> str:
    recorded = False
    for e in phys:
        if not isinstance(e, dict):
            continue
        if (e.get("local") or {}).get("device") != device:
            continue
        pair = _iface_pair_status(e)
        if pair == "not-read":
            continue
        recorded = True
        if pair in {"up/down", "unknown"}:
            return INVENTORY_REVIEW_STATUS
        edge_id = str(e.get("id") or "")
        issue = mismatch_by_edge.get(edge_id)
        if issue and not issue.get("confirmed"):
            return INVENTORY_REVIEW_STATUS
    # Also: mismatch issues for device without edge join
    for issue in mismatch_by_edge.values():
        if issue.get("confirmed"):
            continue
        if issue.get("device") == device:
            return INVENTORY_REVIEW_STATUS
    # No interface read for the device (none in NSO, or the read failed): nothing was checked.
    return "Healthy" if recorded else "—"


def device_notes_label(
    device: str,
    phys: list,
    mismatch_by_edge: dict[str, dict[str, Any]],
) -> str:
    """Short Device Health notes — skip admin-down / quiet down/down.

    Notes call out items that need attention (aligned with Interfaces=Inventory Review):
    ``up/down``, ``unknown``, or unconfirmed NSO↔box mismatches.
    """
    has_up_down = False
    has_unknown = False
    for e in phys:
        if not isinstance(e, dict):
            continue
        if (e.get("local") or {}).get("device") != device:
            continue
        pair = _iface_pair_status(e)
        if pair == "up/down":
            has_up_down = True
        elif pair == "unknown":
            has_unknown = True

    has_mismatch = False
    for edge_id, issue in mismatch_by_edge.items():
        if issue.get("confirmed"):
            continue
        if issue.get("device") == device:
            has_mismatch = True
            break
        # Also tie by edge when local device matches
        for e in phys:
            if not isinstance(e, dict):
                continue
            if str(e.get("id") or "") == edge_id and (
                e.get("local") or {}
            ).get("device") == device:
                has_mismatch = True
                break

    parts: list[str] = []
    if has_up_down:
        parts.append("Interface up/down")
    if has_unknown or has_mismatch:
        parts.append("Mapping unknown")
    if not parts:
        return ""
    # Self-contained note — do not cross-reference "Detailed Analysis"
    # (that section is multi-agent / diagnostic --full only).
    return ", ".join(parts)


def device_sync_label(device: str, sync_map: dict[str, str]) -> str:
    result = sync_map.get(device)
    if not result:
        return "—"
    return str(result)


def build_device_health_rows(
    topology: Any,
    fleet_sync: Any = None,
    hardware_health: Any = None,
) -> list[dict[str, Any]]:
    from nso_facts.hardware_health import device_hardware_label

    op_layers = operational_layers(topology)
    if op_layers is None:
        return []
    static = static_layers(topology)
    phys = merged_edges(op_layers, static, "physical")
    under = merged_edges(op_layers, static, "underlay")
    route = merged_edges(op_layers, static, "routing")
    unmapped_bgp, unmapped_isis = unmapped_by_device(topology)
    mismatch_by_edge = mismatch_by_edge_id(topology)
    sync_map = build_device_sync_map(fleet_sync)
    hw = hardware_health if isinstance(hardware_health, dict) else {}

    devices = sorted(
        devices_in_edges(phys, under, route)
        | set(unmapped_bgp)
        | set(unmapped_isis)
        | set(hw)
    )
    rows: list[dict[str, Any]] = []
    for device in devices:
        bgp_up, bgp_total = peer_up_total(device, route)
        isis_up, isis_total = peer_up_total(device, under)
        rows.append(
            {
                "device": device,
                "sync": device_sync_label(device, sync_map),
                "interfaces": device_interfaces_label(
                    device, phys, mismatch_by_edge
                ),
                "hardware": device_hardware_label(hw.get(device)),
                "bgp": f"{bgp_up}/{bgp_total}" if bgp_total else "—",
                "isis": f"{isis_up}/{isis_total}" if isis_total else "—",
                "notes": device_notes_label(device, phys, mismatch_by_edge),
            }
        )
    return rows


def fleet_inventory_review_count(topology: Any, fleet_sync: Any = None) -> int:
    return sum(
        1
        for row in build_device_health_rows(topology, fleet_sync)
        if row.get("interfaces") == INVENTORY_REVIEW_STATUS
    )
