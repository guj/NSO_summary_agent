"""Fleet-level MCP collect for multi-agent executive sections."""

from __future__ import annotations

from typing import Any, Sequence

from agent.config import Settings
from multi_agent.base import AgentResult
from nso_facts.fact_pack import build_fact_pack
from nso_facts.topology.graph import (
    summarize_static_physical,
    summarize_static_routing,
    summarize_static_underlay,
)
from nso_facts.topology.physical import build_operational_physical_layer


async def collect_fleet_spine(
    client: Any,
    settings: Settings,
    device_names: list[str],
    *,
    physical_edges: list[dict[str, Any]],
    only_service_types: Sequence[str] | None = None,
    only_service_ids: Sequence[str] | None = None,
    lean: bool = False,
) -> dict[str, Any]:
    """Services, fleet sync, CPU/mem, hardware, physical operational.

    ``lean=True`` (service-focus): skip fleet sync, HW, system health, and
    physical operational probes — only fetch the requested service type(s).
    """
    return await build_fact_pack(
        client,
        settings,
        device_names=device_names,
        include_capabilities=False,
        include_system_health=not lean,
        include_hardware_health=not lean,
        # Fleet sync is cheap (one call) and required for service up/unknown
        # classification when check_service_sync shape is ambiguous.
        include_fleet_sync=True,
        physical_edges=physical_edges,
        include_physical_operational=bool(physical_edges) and not lean,
        only_service_types=only_service_types,
        only_service_ids=only_service_ids,
    )


def merge_device_feedback(
    fleet: dict[str, Any],
    device_results: list[AgentResult],
) -> dict[str, Any]:
    """Overlay per-device agent hardware/system onto fleet maps."""
    hw = dict(fleet.get("hardware_health") or {})
    sys = dict(fleet.get("system_health") or {})
    for result in device_results:
        if result.layer != "device":
            continue
        device = (result.seed or {}).get("device") or result.name.removeprefix(
            "device:"
        )
        if result.hardware:
            hw[device] = result.hardware
        sys_extra = (result.seed or {}).get("system_health")
        if isinstance(sys_extra, dict) and sys_extra:
            sys[device] = sys_extra
    out = dict(fleet)
    out["hardware_health"] = hw
    out["system_health"] = sys
    return out


def assemble_topology(
    *,
    device_names: list[str],
    physical_edges: list[dict[str, Any]],
    physical_op_edges: list[dict[str, Any]],
    isis: AgentResult | None,
    bgp: AgentResult | None,
    extra_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a topology object compatible with report_executive helpers."""
    isis = isis or AgentResult(name="isis", layer="underlay")
    bgp = bgp or AgentResult(name="bgp", layer="routing")
    issues = list(isis.issues) + list(bgp.issues) + list(extra_issues or [])
    return {
        "static": {
            "nodes": [{"id": n, "site_id": None} for n in device_names],
            "layers": {
                "physical": {
                    "edges": physical_edges,
                    "summary": summarize_static_physical(physical_edges),
                },
                "underlay": {
                    "edges": isis.static_edges,
                    "summary": summarize_static_underlay(isis.static_edges),
                },
                "routing": {
                    "edges": bgp.static_edges,
                    "summary": summarize_static_routing(bgp.static_edges),
                },
                "services": {"edges": [], "summary": {}},
            },
        },
        "operational": {
            "layers": {
                "physical": build_operational_physical_layer(physical_op_edges),
                "underlay": {
                    "edges": isis.operational_edges,
                    "summary": isis.operational_summary or {},
                },
                "routing": {
                    "edges": bgp.operational_edges,
                    "summary": bgp.operational_summary or {},
                },
                "services": {"edges": [], "summary": {}},
            },
            "issues": issues,
        },
    }
