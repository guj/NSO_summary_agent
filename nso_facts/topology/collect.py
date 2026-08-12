"""Topology collection orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent.config import Settings
from nso_facts.topology.devices import parse_device_names
from nso_facts.topology.graph import empty_operational_layers, empty_static_layers, summarize_issues
from nso_facts.topology.persist import (
    build_static_document,
    load_static_file,
    merge_services_layer,
    save_static_file,
    static_rebuild_needed,
    static_view_from_document,
    topology_force_update,
)
from nso_facts.topology.iface_equiv import (
    SUGGESTED_EQUIVALENCES_DRAFT_REL,
    load_interface_equivalences,
    write_suggested_equivalences_draft,
)
from nso_facts.topology.physical import (
    build_operational_physical_layer,
    collect_operational_physical,
    collect_static_physical,
)
from nso_facts.topology.underlay import (
    build_operational_underlay_layer,
    collect_operational_underlay,
    collect_static_underlay,
)
from nso_facts.topology.routing import (
    build_operational_routing_layer,
    collect_operational_routing,
    collect_static_routing,
)
from nso_facts.topology.route_summary import collect_route_summaries
from nso_facts.topology.services import (
    build_operational_service_edges,
    build_operational_services_layer,
    build_static_service_edges,
    build_static_services_layer,
)
from nso_facts.mcp_client import call_mcp


async def load_or_build_topology(
    client: Any,
    settings: Settings,
    *,
    services_by_type: dict[str, Any] | None = None,
    services: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return snapshot['topology'] with static + operational views."""
    list_result = await call_mcp(client, "list_devices")
    device_names = parse_device_names(list_result)

    force = topology_force_update(settings)
    rebuild, build_reason = static_rebuild_needed(
        settings.state_dir, device_names, force=force
    )

    svc_static, svc_static_issues = build_static_service_edges(
        services_by_type or {},
        services=services,
    )
    svc_op, svc_op_issues = build_operational_service_edges(
        svc_static, services or {}
    )

    static_issues: list[dict[str, Any]] = []
    coverage: dict[str, int] = {
        "devices_total": len(device_names),
        "devices_queried": 0,
        "devices_failed": 0,
    }

    if rebuild:
        physical_edges, phys_static_issues, static_cov = await collect_static_physical(
            client, device_names
        )
        underlay_edges, under_static_issues, under_static_cov = (
            await collect_static_underlay(
                client, device_names, physical_edges=physical_edges
            )
        )
        routing_edges, route_static_issues, route_static_cov = (
            await collect_static_routing(client, device_names)
        )
        document = build_static_document(
            device_names=device_names,
            physical_edges=physical_edges,
            underlay_edges=underlay_edges,
            routing_edges=routing_edges,
            services_edges=svc_static,
            build_reason=build_reason,
        )
        save_static_file(settings.state_dir, document)
        static_view = static_view_from_document(document)
        static_built_at = document["built_at"]
        static_source = "fresh"
        static_issues = phys_static_issues + under_static_issues + route_static_issues
        coverage = _merge_coverage(coverage, static_cov)
        coverage = _merge_coverage(coverage, under_static_cov)
        coverage = _merge_coverage(coverage, route_static_cov)
    else:
        document = load_static_file(settings.state_dir) or {}
        merge_services_layer(document, svc_static)
        save_static_file(settings.state_dir, document)
        static_view = static_view_from_document(document)
        static_built_at = document.get("built_at")
        static_source = "cache"
        layers = static_view.get("layers") or {}
        physical_edges = layers.get("physical", {}).get("edges", [])
        underlay_edges = layers.get("underlay", {}).get("edges", [])
        routing_edges = layers.get("routing", {}).get("edges", [])

    # Ensure in-memory view always has fresh services (even if layers dict was shared)
    layers = dict(static_view.get("layers") or empty_static_layers())
    layers["services"] = build_static_services_layer(svc_static)
    static_view = {**static_view, "layers": layers}

    equivalences = load_interface_equivalences(settings.interface_equivalences_file)
    op_phys, phys_op_issues, phys_op_cov = await collect_operational_physical(
        client, physical_edges, equivalences=equivalences
    )
    op_under, under_op_issues, under_op_cov = await collect_operational_underlay(
        client,
        device_names,
        underlay_edges,
        physical_edges=physical_edges,
    )
    op_route, route_op_issues, route_op_cov = await collect_operational_routing(
        client, device_names, routing_edges
    )
    route_summaries, route_sum_issues, route_sum_cov = await collect_route_summaries(
        client, device_names
    )
    coverage = _merge_coverage(coverage, phys_op_cov)
    coverage = _merge_coverage(coverage, under_op_cov)
    coverage = _merge_coverage(coverage, route_op_cov)
    coverage = _merge_coverage(coverage, route_sum_cov)

    issues = (
        static_issues
        + phys_op_issues
        + under_op_issues
        + route_op_issues
        + route_sum_issues
        + svc_static_issues
        + svc_op_issues
    )
    draft_path = (
        settings.prompts_dir.parent / SUGGESTED_EQUIVALENCES_DRAFT_REL
    )
    try:
        write_suggested_equivalences_draft(draft_path, issues)
    except OSError:
        pass
    operational_layers = empty_operational_layers()
    operational_layers["physical"] = build_operational_physical_layer(op_phys)
    operational_layers["underlay"] = build_operational_underlay_layer(op_under)
    operational_layers["routing"] = build_operational_routing_layer(op_route)
    operational_layers["services"] = build_operational_services_layer(svc_op)

    return {
        "static_source": static_source,
        "static_built_at": static_built_at,
        "operational_refreshed_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "coverage": coverage,
        "static": static_view,
        "operational": {
            "layers": operational_layers,
            "route_summary": route_summaries,
            "issues": issues,
            "summary": summarize_issues(issues),
        },
    }


def _merge_coverage(
    base: dict[str, int],
    extra: dict[str, int],
) -> dict[str, int]:
    return {
        "devices_total": max(
            base.get("devices_total", 0), extra.get("devices_total", 0)
        ),
        "devices_queried": max(
            base.get("devices_queried", 0), extra.get("devices_queried", 0)
        ),
        "devices_failed": max(
            base.get("devices_failed", 0), extra.get("devices_failed", 0)
        ),
    }
