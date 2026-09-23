"""Expand ISIS/BGP collection for service-focus runs when evidence needs it."""

from __future__ import annotations

import sys
from typing import Any

from diagnostic_mas.case import CaseFile
from diagnostic_mas.dataplane_verify import iter_service_records
from diagnostic_mas.focus import endpoint_devices_from_services
from diagnostic_mas.ingest import ingest_layer_spine
from diagnostic_mas.roles.bgp import run_bgp_spine
from diagnostic_mas.roles.isis import run_isis_spine
from diagnostic_mas.device_health import services_from_case


def _diagnosis_suggests_underlay(case: CaseFile) -> bool:
    needles = (
        "underlay",
        "isis",
        "bgp",
        "peer",
        "loopback",
        "unreachable",
        "adjacency",
        "remote unset",
        "remote none",
        "seg2",
        "segment-2",
        "evpn",
    )
    for dx in case.diagnoses:
        if dx.get("kind") != "dataplane":
            continue
        blob = " ".join(
            str(dx.get(k) or "") for k in ("observed", "cause", "fix_suggestion")
        ).lower()
        if any(n in blob for n in needles):
            return True
        if dx.get("complete") is False:
            # Incomplete verify on L2 often needs peer/underlay context next.
            subject = dx.get("subject") if isinstance(dx.get("subject"), dict) else {}
            stype = str(subject.get("service_type") or "").lower()
            if stype.startswith("l2"):
                return True
    for issue in case.issues:
        code = str(issue.get("code") or "").lower()
        if "degraded" in code or "down" in code:
            live = issue.get("live_l2") if isinstance(issue.get("live_l2"), dict) else {}
            for ep in live.get("endpoints") or []:
                if not isinstance(ep, dict):
                    continue
                st = str(ep.get("st") or "").upper()
                ac_st = str(ep.get("ac_st") or "").upper()
                # XC DN while AC looks up → underlay / remote dig
                if st == "DN" and ac_st in {"UP", ""}:
                    return True
    return False


async def maybe_expand_underlay_for_service_focus(
    client: Any,
    case: CaseFile,
    *,
    physical_edges: list[dict[str, Any]] | None = None,
) -> list[str]:
    """If service evidence calls for it, collect ISIS+BGP on endpoint devices.

    Returns the device list used (empty if skipped).
    """
    if not _diagnosis_suggests_underlay(case):
        print(
            "[focus] underlay expand skipped (no service evidence calling for it)",
            file=sys.stderr,
        )
        return []

    services = services_from_case(case)
    devices = list(case.focus_devices or []) or endpoint_devices_from_services(
        services
    )
    if not devices:
        # Fall back to devices named on service records via iter
        for _ev, rec in iter_service_records(case):
            for d in rec.get("devices") or []:
                if isinstance(d, str) and d.strip() and d not in devices:
                    devices.append(d.strip())
    if not devices:
        print("[focus] underlay expand skipped (no endpoint devices)", file=sys.stderr)
        return []

    print(
        f"[focus] expanding ISIS+BGP on endpoint devices={devices}",
        file=sys.stderr,
    )
    edges = list(physical_edges or [])
    isis = await run_isis_spine(client, devices, edges)
    ingest_layer_spine(
        case,
        layer="underlay",
        role="isis",
        static_summary=isis.get("static_summary") or {},
        operational_summary=isis.get("operational_summary") or {},
        issues=list(isis.get("issues") or []),
        static_edges=isis.get("static_edges"),
        operational_edges=isis.get("operational_edges"),
        extra={"coverage": isis.get("coverage"), "service_focus_expand": True},
    )
    bgp = await run_bgp_spine(client, devices)
    ingest_layer_spine(
        case,
        layer="routing",
        role="bgp",
        static_summary=bgp.get("static_summary") or {},
        operational_summary=bgp.get("operational_summary") or {},
        issues=list(bgp.get("issues") or []),
        static_edges=bgp.get("static_edges"),
        operational_edges=bgp.get("operational_edges"),
        extra={"coverage": bgp.get("coverage"), "service_focus_expand": True},
    )
    # Prefer reporting those endpoints
    if not case.focus_devices:
        case.focus_devices = list(devices)
    case.device_names = list(dict.fromkeys([*(case.device_names or []), *devices]))
    return devices
