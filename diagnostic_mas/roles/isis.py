"""ISIS role: underlay spine via nso_facts."""

from __future__ import annotations

from typing import Any

from multi_agent.base import dedupe_issues
from nso_facts.topology.graph import (
    summarize_operational_underlay,
    summarize_static_underlay,
)
from nso_facts.topology.underlay import (
    collect_operational_underlay,
    collect_static_underlay,
)


def expected_adjacencies(previous_case: dict[str, Any] | None) -> list[dict[str, Any]]:
    """IS-IS adjacencies the last published scan saw up, or already held Down as missing.

    ``held`` marks the second kind. Adjacencies the routers themselves reported
    down are not expected: they were not up to begin with.
    """
    for ev in (previous_case or {}).get("evidence") or []:
        if not isinstance(ev, dict) or ev.get("kind") != "spine" or ev.get("role") != "isis":
            continue
        payload = ev.get("payload") or {}
        status = {str(edge.get("id")): str((edge.get("state") or {}).get("status") or "")
                  for edge in payload.get("operational_edges") or [] if isinstance(edge, dict)}
        out = []
        for edge in payload.get("static_edges") or []:
            if not isinstance(edge, dict) or not edge.get("id"):
                continue
            held = bool((edge.get("meta") or {}).get("expected")) and status.get(edge["id"]) != "up"
            if status.get(edge["id"]) == "up" or held:
                out.append({"id": edge["id"], "local": edge.get("local") or {},
                            "remote": edge.get("remote") or {}, "held": held})
        return out
    return []


async def run_isis_spine(
    client: Any,
    device_names: list[str],
    physical_edges: list[dict[str, Any]],
    *,
    expected_edges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    static_edges, static_issues, static_cov = await collect_static_underlay(
        client, device_names, physical_edges=physical_edges, expected_edges=expected_edges
    )
    op_edges, op_issues, op_cov = await collect_operational_underlay(
        client,
        device_names,
        static_edges,
        physical_edges=physical_edges,
    )
    issues = dedupe_issues(static_issues + op_issues)
    return {
        "static_summary": summarize_static_underlay(static_edges),
        "operational_summary": summarize_operational_underlay(op_edges),
        "issues": issues,
        "static_edges": static_edges,
        "operational_edges": op_edges,
        "coverage": {
            "devices_total": max(
                static_cov.get("devices_total", 0), op_cov.get("devices_total", 0)
            ),
            "devices_queried": max(
                static_cov.get("devices_queried", 0),
                op_cov.get("devices_queried", 0),
            ),
            "devices_failed": max(
                static_cov.get("devices_failed", 0),
                op_cov.get("devices_failed", 0),
            ),
        },
    }
