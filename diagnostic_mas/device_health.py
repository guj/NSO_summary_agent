"""Device Health table + Detailed Device Analysis for diagnostic_mas reports."""

from __future__ import annotations

from typing import Any

from diagnostic_mas.case import CaseFile
from nso_facts.topology.physical import build_operational_physical_layer
from nso_report.devices import format_devices_section
from nso_report.executive import (
    build_device_health_rows,
    format_device_health_table,
)


def _spine_payload(case: CaseFile, role: str) -> dict[str, Any]:
    for ev in case.evidence:
        if ev.get("kind") == "spine" and ev.get("role") == role:
            payload = ev.get("payload")
            return payload if isinstance(payload, dict) else {}
    return {}


def _normalize_edge(edge: Any) -> Any:
    """Ensure edges expose local/remote.device (report helpers ignore endpoints)."""
    if not isinstance(edge, dict):
        return edge
    local = edge.get("local")
    if isinstance(local, dict) and local.get("device"):
        return edge
    endpoints = edge.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        return edge
    out = dict(edge)
    a = endpoints[0] if isinstance(endpoints[0], dict) else {}
    out["local"] = {
        k: a[k]
        for k in ("device", "interface", "address")
        if isinstance(a.get(k), str) and a.get(k)
    }
    if len(endpoints) >= 2 and isinstance(endpoints[1], dict):
        b = endpoints[1]
        out["remote"] = {
            k: b[k]
            for k in ("device", "interface", "address")
            if isinstance(b.get(k), str) and b.get(k)
        }
    return out


def _normalize_edges(edges: list[Any]) -> list[Any]:
    return [_normalize_edge(e) for e in edges]


def _normalize_fleet_sync(fleet_sync: Any) -> Any:
    """Accept MCP ``device``/``result`` or alternate ``name``/``sync_state`` rows."""
    if not isinstance(fleet_sync, dict) or fleet_sync.get("status") != "success":
        return fleet_sync
    data = fleet_sync.get("data") if isinstance(fleet_sync.get("data"), dict) else {}
    devices = data.get("devices")
    if not isinstance(devices, list):
        return fleet_sync
    normalized: list[dict[str, str]] = []
    for row in devices:
        if not isinstance(row, dict):
            continue
        device = row.get("device") if row.get("device") is not None else row.get("name")
        result = (
            row.get("result") if row.get("result") is not None else row.get("sync_state")
        )
        if device is None or result is None:
            continue
        normalized.append({"device": str(device), "result": str(result)})
    if not normalized:
        return fleet_sync
    out = dict(fleet_sync)
    out["data"] = {**data, "devices": normalized}
    return out


def topology_from_case(case: CaseFile) -> dict[str, Any]:
    """Minimal topology blob compatible with nso_report device helpers."""
    phys = _spine_payload(case, "physical")
    isis = _spine_payload(case, "isis")
    bgp = _spine_payload(case, "bgp")
    svc = _spine_payload(case, "service")
    extra = svc.get("extra") if isinstance(svc.get("extra"), dict) else {}

    phys_static = _normalize_edges(list(phys.get("static_edges") or []))
    phys_op = _normalize_edges(
        list(
            extra.get("physical_operational_edges")
            or phys.get("operational_edges")
            or []
        )
    )
    under_static = _normalize_edges(list(isis.get("static_edges") or []))
    under_op = _normalize_edges(list(isis.get("operational_edges") or []))
    route_static = _normalize_edges(list(bgp.get("static_edges") or []))
    route_op = _normalize_edges(list(bgp.get("operational_edges") or []))

    nodes = [{"id": n, "site_id": None} for n in (case.device_names or [])]
    return {
        "static": {
            "nodes": nodes,
            "layers": {
                "physical": {"edges": phys_static, "summary": {}},
                "underlay": {"edges": under_static, "summary": {}},
                "routing": {"edges": route_static, "summary": {}},
                "services": {"edges": [], "summary": {}},
            },
        },
        "operational": {
            "layers": {
                "physical": build_operational_physical_layer(phys_op),
                "underlay": {
                    "edges": under_op,
                    "summary": isis.get("operational_summary") or {},
                },
                "routing": {
                    "edges": route_op,
                    "summary": bgp.get("operational_summary") or {},
                },
                "services": {"edges": [], "summary": {}},
            },
            "issues": list(extra.get("physical_issues") or []),
        },
    }


def fleet_maps_from_case(
    case: CaseFile,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Return (fleet_sync, hardware_health, system_health) from service spine."""
    svc = _spine_payload(case, "service")
    extra = svc.get("extra") if isinstance(svc.get("extra"), dict) else {}
    return (
        _normalize_fleet_sync(extra.get("fleet_sync")),
        dict(extra.get("hardware_health") or {}),
        dict(extra.get("system_health") or {}),
    )


def services_from_case(case: CaseFile) -> dict[str, Any]:
    svc = _spine_payload(case, "service")
    extra = svc.get("extra") if isinstance(svc.get("extra"), dict) else {}
    services = extra.get("services")
    return services if isinstance(services, dict) else {}


def format_device_health_section(case: CaseFile) -> list[str]:
    """Lines for ## Device Health (same columns as nso-summary-run)."""
    topology = topology_from_case(case)
    fleet_sync, hardware_health, _system = fleet_maps_from_case(case)
    rows = build_device_health_rows(
        topology, fleet_sync, hardware_health=hardware_health
    )
    focus = list(case.focus_devices or []) or list(case.device_names or [])
    # Seed rows for known/focus devices with no topology/hw yet
    seen = {str(r.get("device")) for r in rows}
    for name in focus:
        if name in seen:
            continue
        rows.append(
            {
                "device": name,
                "sync": "—",
                "interfaces": "—",
                "hardware": "Unavailable",
                "bgp": "—",
                "isis": "—",
                "routes": "—",
                "notes": "",
            }
        )
        seen.add(name)
    if case.focus_devices:
        allow = set(case.focus_devices)
        rows = [r for r in rows if str(r.get("device")) in allow]
    rows.sort(key=lambda r: str(r.get("device") or ""))
    if not rows and not hardware_health and not case.device_names:
        return ["(none — run service spine for sync/HW; ISIS/BGP for peers)"]
    table = format_device_health_table(rows)
    # format_device_health_table indents with two spaces; strip for our report
    return [line[2:] if line.startswith("  ") else line for line in table.splitlines()]


def format_detailed_devices_section(case: CaseFile) -> list[str]:
    """Lines for ## Detailed Device Analysis (same body as nso-summary-run)."""
    topology = topology_from_case(case)
    fleet_sync, hardware_health, _system = fleet_maps_from_case(case)
    only = list(case.focus_devices or []) or None
    body = format_devices_section(
        topology,
        services=services_from_case(case),
        fleet_sync=fleet_sync,
        hardware_health=hardware_health,
        extra_devices=list(case.device_names or []),
        only_devices=only,
    )
    if body == "No device topology in snapshot.":
        return ["(none — need ISIS/BGP edges, hardware, or device_names)"]
    return body.splitlines()
