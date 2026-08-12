"""Shared fact-pack collect: services, sync, device health, optional physical op."""

from __future__ import annotations

from typing import Any, Protocol

from nso_facts.health import (
    build_device_sync_map,
    derive_counts,
    sync_module_from_type,
)
from nso_facts.hardware_health import collect_hardware_health
from nso_facts.mcp_client import call_mcp
from nso_facts.service_collect import (
    collect_service_health,
    extract_service_type_names,
    normalize_service_type,
    select_service_types,
)
from nso_facts.system_health import collect_system_health
from nso_facts.topology.devices import parse_device_names
from nso_facts.topology.iface_equiv import load_interface_equivalences
from nso_facts.topology.physical import collect_operational_physical


class FactCollectSettings(Protocol):
    ignore_service_types: frozenset[str]
    max_service_types: int


async def collect_services_fact_slice(
    client: Any,
    settings: FactCollectSettings,
) -> dict[str, Any]:
    """Service types, fleet sync, instances, health records, and counts."""
    service_types = await call_mcp(client, "get_service_types")
    fleet_sync = await call_mcp(client, "get_fleet_sync_summary")

    services_by_type: dict[str, Any] = {}
    sync_modules: dict[str, str] = {}
    type_names = select_service_types(
        extract_service_type_names(service_types),
        settings.ignore_service_types,
    )
    for stype in type_names[: settings.max_service_types]:
        query_type = normalize_service_type(stype)
        sync_modules[query_type] = sync_module_from_type(stype)
        try:
            services_by_type[query_type] = await call_mcp(
                client, "get_services", {"service_type": query_type}
            )
        except Exception as exc:  # noqa: BLE001 — partial snapshot
            services_by_type[query_type] = {"error": str(exc)}

    device_sync_map = build_device_sync_map(fleet_sync)
    services = await collect_service_health(
        client, services_by_type, sync_modules, device_sync_map
    )
    return {
        "service_types": service_types,
        "ignored_service_types": sorted(settings.ignore_service_types),
        "fleet_sync": fleet_sync,
        "services_by_type": services_by_type,
        "services": services,
        "counts": derive_counts(services_by_type, services),
    }


async def collect_device_health_maps(
    client: Any,
    device_names: list[str],
    *,
    system: bool = True,
    hardware: bool = True,
) -> dict[str, Any]:
    """Per-device CPU/mem and/or hardware maps (empty dicts when skipped)."""
    system_health: dict[str, Any] = {}
    hardware_health: dict[str, Any] = {}
    if system and device_names:
        system_health = await collect_system_health(client, device_names)
    if hardware and device_names:
        hardware_health = await collect_hardware_health(client, device_names)
    return {
        "system_health": system_health,
        "hardware_health": hardware_health,
    }


async def collect_physical_operational_slice(
    client: Any,
    physical_edges: list[dict[str, Any]],
    *,
    equivalences: Any = None,
) -> dict[str, Any]:
    """Live physical layer edges + issues (same equiv map as summary-run)."""
    op_phys, phys_issues, _ = await collect_operational_physical(
        client, physical_edges, equivalences=equivalences
    )
    return {
        "physical_operational_edges": op_phys,
        "physical_issues": phys_issues,
    }


async def build_fact_pack(
    client: Any,
    settings: FactCollectSettings,
    *,
    device_names: list[str] | None = None,
    include_capabilities: bool = False,
    include_system_health: bool = True,
    include_hardware_health: bool = True,
    physical_edges: list[dict[str, Any]] | None = None,
    include_physical_operational: bool = False,
) -> dict[str, Any]:
    """Compose the shared non-LLM fact pack used by production and multi-agent.

    Does **not** build full topology (callers use ``load_or_build_topology`` or
    multi-agent ``assemble_topology``). Does **not** open an MCP session.
    """
    pack = await collect_services_fact_slice(client, settings)

    if include_capabilities:
        pack["capabilities"] = await call_mcp(client, "get_nso_capabilities")

    names = list(device_names) if device_names is not None else None
    if names is None and (include_system_health or include_hardware_health):
        list_result = await call_mcp(client, "list_devices")
        names = parse_device_names(list_result)
    if names is not None:
        pack["device_names"] = names

    health = await collect_device_health_maps(
        client,
        names or [],
        system=include_system_health,
        hardware=include_hardware_health,
    )
    pack.update(health)

    if include_physical_operational:
        if physical_edges is None:
            raise ValueError(
                "physical_edges required when include_physical_operational=True"
            )
        equivalences = load_interface_equivalences(
            getattr(settings, "interface_equivalences_file", None)
        )
        pack.update(
            await collect_physical_operational_slice(
                client, physical_edges, equivalences=equivalences
            )
        )

    return pack
