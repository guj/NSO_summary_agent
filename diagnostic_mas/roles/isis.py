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
    prefer_later_reading,
)


def expected_adjacencies(previous_case: dict[str, Any] | None) -> list[dict[str, Any]]:
    """IS-IS adjacencies the last published scan saw up, or already held Down as missing.

    ``held`` marks the second kind, with ``since`` the scan it first went missing in.
    One up at the only end that scan read counts as up, with ``listed_by`` that end.
    Adjacencies the routers themselves reported down are not expected: they were
    not up to begin with.
    """
    previous_case = previous_case or {}
    for ev in previous_case.get("evidence") or []:
        if not isinstance(ev, dict) or ev.get("kind") != "spine" or ev.get("role") != "isis":
            continue
        payload = ev.get("payload") or {}
        states = {str(edge.get("id")): edge.get("state") or {}
                  for edge in payload.get("operational_edges") or [] if isinstance(edge, dict)}
        out = []
        for edge in payload.get("static_edges") or []:
            if not isinstance(edge, dict) or not edge.get("id"):
                continue
            meta, state = edge.get("meta") or {}, states.get(edge["id"]) or {}
            status = str(state.get("status") or "")
            held = bool(meta.get("expected")) and status != "up"
            since = (meta.get("missing_since") or previous_case.get("run_id")) if held else None
            listed_by = [str((edge.get(side) or {}).get("device")) for side in ("local", "remote")
                         if state.get(side) == "up"] if status == "unknown" else []
            if status == "up" or held or len(listed_by) == 1:
                out.append({"id": edge["id"], "local": edge.get("local") or {},
                            "remote": edge.get("remote") or {}, "held": held,
                            **({"since": since} if since else {}),
                            **({"listed_by": listed_by[0]} if not held and len(listed_by) == 1 else {})})
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
    # The operational pass read the devices later: what it read stands for a device the first could not.
    static_edges, static_issues, op_issues, failed_then_read = prefer_later_reading(
        static_edges, static_issues, op_edges, op_issues)
    issues = dedupe_issues(static_issues + op_issues)
    return {
        "static_summary": summarize_static_underlay(static_edges),
        "operational_summary": summarize_operational_underlay(op_edges),
        "issues": issues,
        "static_edges": static_edges,
        "operational_edges": op_edges,
        "failed_then_read": failed_then_read,
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
