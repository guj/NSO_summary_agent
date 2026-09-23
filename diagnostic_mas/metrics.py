"""Build Phase-1 Pushgateway snapshots from a diagnostic MAS case."""

from __future__ import annotations

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


def metrics_snapshot_from_case(case: CaseFile) -> dict[str, Any]:
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

    return {
        "topology": topology,
        "fleet_sync": fleet_sync,
        "counts": counts,
        "system_health": system_health,
        "hardware_health": hardware_health,
        "delta": None,
    }
