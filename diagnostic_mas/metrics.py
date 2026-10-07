"""Build Phase-1 Pushgateway snapshots from a diagnostic MAS case."""

from __future__ import annotations

from collections import Counter
from typing import Any

from diagnostic_mas.case import CaseFile
from diagnostic_mas.device_health import (
    fleet_maps_from_case,
    services_from_case,
    topology_from_case,
)
from diagnostic_mas.focus import counts_from_services


def _issue_dedupe_key(issue: dict[str, Any]) -> tuple[Any, ...]:
    return (
        issue.get("layer"),
        issue.get("code"),
        issue.get("edge_id"),
        issue.get("message"),
    )


def _final_status_metrics(case: CaseFile) -> tuple[dict[str, dict[str, int]], list[dict[str, Any]]]:
    """The report's final status per service type, and non-OpUp services per device.

    A service counts under each device its fault points to (the device whose
    basic check failed, else every endpoint), so device rows can overlap.
    """
    from diagnostic_mas.operator_report import _fault_location
    from diagnostic_mas.service_final_status import STATUSES, final_service_assessments

    by_type: dict[str, dict[str, int]] = {}
    by_device: Counter[tuple[str, str, str]] = Counter()
    for kind, _name, service, status, _reason in final_service_assessments(case):
        by_type.setdefault(kind, dict.fromkeys(STATUSES, 0))[status] += 1
        if status != "up":
            for device in _fault_location(service)[0]:
                by_device[device, kind, status] += 1
    faults = [
        {"device": device, "service_type": kind, "status": status, "count": count}
        for (device, kind, status), count in sorted(by_device.items())
    ]
    return by_type, faults


def metrics_snapshot_from_case(
    case: CaseFile, *, run_id: str | None = None, report_url: str | None = None
) -> dict[str, Any]:
    """Phase-1-compatible snapshot for ``push_phase1_metrics``."""
    topology = topology_from_case(case)
    fleet_sync, hardware_health, system_health = fleet_maps_from_case(case)
    services = services_from_case(case)
    counts = counts_from_services(services)

    # Physical issues live on topology; ISIS/BGP/service issues are opened onto
    # ``case.issues`` by ``ingest_layer_spine`` (not stored in spine payloads).
    merged_issues: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    op = topology.get("operational") if isinstance(topology.get("operational"), dict) else {}
    for existing in op.get("issues") or []:
        if not isinstance(existing, dict):
            continue
        row = dict(existing)
        key = _issue_dedupe_key(row)
        if key in seen:
            continue
        seen.add(key)
        merged_issues.append(row)
    for issue in case.issues:
        if not isinstance(issue, dict):
            continue
        row = dict(issue)
        key = _issue_dedupe_key(row)
        if key in seen:
            continue
        seen.add(key)
        merged_issues.append(row)
    if isinstance(op, dict):
        op = dict(op)
        op["issues"] = merged_issues
        topology = dict(topology)
        topology["operational"] = op

    from diagnostic_mas.roles.summary import _collection_gap_devices

    service_status, service_faults = _final_status_metrics(case)
    snapshot: dict[str, Any] = {
        "topology": topology,
        "fleet_sync": fleet_sync,
        "counts": counts,
        "system_health": system_health,
        "hardware_health": hardware_health,
        "delta": None,
        "service_status": service_status,
        "service_faults": service_faults,
        "devices_not_covered": len(_collection_gap_devices(case)),
    }
    if run_id:
        snapshot["scan"] = {"run_id": run_id, "report_url": report_url or ""}
    return snapshot
