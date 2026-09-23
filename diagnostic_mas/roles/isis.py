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


async def run_isis_spine(
    client: Any,
    device_names: list[str],
    physical_edges: list[dict[str, Any]],
) -> dict[str, Any]:
    static_edges, static_issues, static_cov = await collect_static_underlay(
        client, device_names, physical_edges=physical_edges
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
