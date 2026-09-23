"""Service role helpers and spine."""

from __future__ import annotations

from typing import Any

from agent.config import Settings
from diagnostic_mas.focus import (
    counts_from_services,
    filter_device_map,
    filter_services,
)
from multi_agent.fleet_spine import collect_fleet_spine


def format_live_l2_cause(live_l2: dict[str, Any] | None) -> str:
    """Human-readable XC/AC/seg2 line from live_l2 endpoints (empty if none)."""
    if not isinstance(live_l2, dict):
        return ""
    parts: list[str] = []
    for ep in live_l2.get("endpoints") or []:
        if not isinstance(ep, dict):
            continue
        device = str(ep.get("device") or "").strip()
        ac = str(ep.get("ac") or "").strip()
        st = ep.get("st")
        if st is None or st == "":
            err = ep.get("error")
            xc_s = f"unknown({err})" if err else "unknown"
        else:
            xc_s = str(st).upper()
        if not device and not ac:
            continue
        label = " ".join(x for x in (device, ac) if x)
        bits = [f"{label} XC {xc_s}"]
        ac_st = ep.get("ac_st")
        if ac_st is not None and str(ac_st).strip() != "":
            bits.append(f"AC {str(ac_st).upper()}")
        seg2_st = ep.get("seg2_st")
        if seg2_st is not None and str(seg2_st).strip() != "":
            bits.append(f"seg2 {str(seg2_st).upper()}")
        seg2 = ep.get("seg2")
        if isinstance(seg2, str) and seg2.strip():
            bits.append(seg2.strip())
        parts.append(", ".join(bits))
    return "; ".join(parts)


def issues_from_service_health(services: dict[str, Any]) -> list[dict[str, Any]]:
    """Open collector-style issues for degraded/down service instances.

    Accepts either:
    - flat map from ``collect_service_health``: ``{key: {name, status, ...}}``
    - nested ``{stype: {instances: [...]}}`` for tests / alternate shapes
    """
    out: list[dict[str, Any]] = []
    for key, group in (services or {}).items():
        if not isinstance(group, dict):
            continue
        if "instances" in group:
            stype = str(key)
            for inst in group.get("instances") or []:
                if isinstance(inst, dict):
                    _append_service_issue(out, stype, inst)
            continue
        if "status" in group or "name" in group:
            stype = str(group.get("service_type") or key.split("/", 1)[0])
            _append_service_issue(out, stype, group)
    return out


def _append_service_issue(
    out: list[dict[str, Any]], stype: str, inst: dict[str, Any]
) -> None:
    status = (inst.get("status") or "").lower()
    if status not in {"down", "degraded"}:
        return
    name = inst.get("name") or inst.get("id")
    live_l2 = inst.get("live_l2") if isinstance(inst.get("live_l2"), dict) else None
    cause = format_live_l2_cause(live_l2)
    message = f"{stype} {name}: {status}"
    sys_s = inst.get("system_status")
    dp_s = inst.get("dataplane_status")
    if sys_s or dp_s:
        message = (
            f"{message} (system={sys_s or '?'}, dataplane={dp_s or '?'})"
        )
    if cause:
        message = f"{message} — {cause}"
    rec: dict[str, Any] = {
        "severity": "high" if status == "down" else "medium",
        "layer": "services",
        "code": f"service_{status}",
        "edge_id": name,
        "message": message,
        "devices": list(inst.get("devices") or []),
    }
    if live_l2 is not None:
        rec["live_l2"] = live_l2
    if "in_sync" in inst:
        rec["in_sync"] = inst.get("in_sync")
    if isinstance(inst.get("device_sync"), dict):
        rec["device_sync"] = dict(inst["device_sync"])
    if inst.get("system_status"):
        rec["system_status"] = inst.get("system_status")
    if inst.get("dataplane_status"):
        rec["dataplane_status"] = inst.get("dataplane_status")
    out.append(rec)


async def run_service_spine(
    client: Any,
    settings: Settings,
    device_names: list[str],
    physical_edges: list[dict[str, Any]],
    *,
    service_type: str | None = None,
    service_id: str | None = None,
    filter_to_devices: bool = False,
) -> dict[str, Any]:
    # Focused type/id → lean MCP (no fleet-wide type walk / HW / phys op)
    lean = bool(service_type or service_id)
    only_types = [service_type] if service_type else None
    only_ids = [service_id] if service_id else None

    pack = await collect_fleet_spine(
        client,
        settings,
        device_names,
        physical_edges=physical_edges,
        only_service_types=only_types,
        only_service_ids=only_ids,
        lean=lean,
    )
    device_filter = list(device_names) if filter_to_devices else None
    services = filter_services(
        pack.get("services") or {},
        service_type=service_type or None,
        service_id=service_id or None,
        devices=device_filter,
    )
    counts = counts_from_services(services)
    issues = issues_from_service_health(services)
    hw = filter_device_map(pack.get("hardware_health"), device_names)
    sys_h = filter_device_map(pack.get("system_health"), device_names)
    extra: dict[str, Any] = {
        "services": services,
        "fleet_sync": pack.get("fleet_sync"),
        "hardware_health": hw,
        "system_health": sys_h,
        "physical_operational_edges": pack.get("physical_operational_edges")
        or [],
        "physical_issues": pack.get("physical_issues") or [],
        "counts": counts,
        "lean": lean,
    }
    note = pack.get("service_sync_note")
    if isinstance(note, str) and note.strip():
        extra["service_sync_note"] = note.strip()
    return {
        "static_summary": {"service_keys": len(services)},
        "operational_summary": counts,
        "issues": issues,
        "static_edges": [],
        "operational_edges": [],
        "extra": extra,
    }
