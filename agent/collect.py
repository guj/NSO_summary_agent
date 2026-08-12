"""Collect NSO health data via MCP tools (deterministic, no LLM)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent.config import Settings
from agent.mcp_client import mcp_session
from agent.topology import load_or_build_topology
from nso_facts.fact_pack import build_fact_pack
from nso_facts.service_collect import (
    collect_service_health,
    extract_service_type_names,
    is_ignored_service_type as _is_ignored_service_type,
    normalize_service_type,
    select_service_types,
)

__all__ = [
    "collect_snapshot",
    "normalize_service_type",
]

# Re-exports for callers that imported helpers from agent.collect
_collect_service_health = collect_service_health
_extract_service_type_names = extract_service_type_names
_select_service_types = select_service_types


async def collect_snapshot(
    settings: Settings | None = None,
    *,
    skip_llm: bool = False,
) -> dict[str, Any]:
    """Run a fixed set of MCP tools and return a structured snapshot."""
    from agent.config import load_settings

    s = settings or load_settings()
    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    need_sys = (
        "executive" in s.report_sections or "system_health" in s.report_sections
    )
    need_hw = "executive" in s.report_sections or "devices" in s.report_sections

    async with mcp_session(s) as client:
        pack = await build_fact_pack(
            client,
            s,
            include_capabilities=True,
            include_system_health=need_sys,
            include_hardware_health=need_hw,
        )
        topology = await load_or_build_topology(
            client,
            s,
            services_by_type=pack.get("services_by_type") or {},
            services=pack.get("services") or {},
        )

        if (
            not skip_llm
            and "devices" in s.report_sections
            and not s.iface_troubleshoot_disable
            and isinstance(topology, dict)
        ):
            from agent.iface_troubleshoot import collect_interface_troubleshooting

            await collect_interface_troubleshooting(client, s, topology)

    return {
        "run_id": run_id,
        "service_types": pack.get("service_types"),
        "ignored_service_types": pack.get("ignored_service_types")
        or sorted(s.ignore_service_types),
        "services_by_type": pack.get("services_by_type") or {},
        "fleet_sync": pack.get("fleet_sync"),
        "capabilities": pack.get("capabilities"),
        "counts": pack.get("counts") or {},
        "services": pack.get("services") or {},
        "topology": topology,
        "system_health": pack.get("system_health") or {},
        "hardware_health": pack.get("hardware_health") or {},
    }
