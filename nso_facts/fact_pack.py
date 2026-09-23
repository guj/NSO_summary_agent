"""Shared fact-pack collect: services, sync, device health, optional physical op."""

from __future__ import annotations

from typing import Any, Protocol, Sequence

from nso_facts.health import (
    build_device_sync_map,
    derive_counts,
    extract_service_instances,
    instance_name,
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
    service_sync_mode: str


def _filter_services_payload_by_ids(
    services_by_type: dict[str, Any],
    only_ids: Sequence[str],
) -> dict[str, Any]:
    """Keep only instances whose name matches any only_ids substring/exact."""
    wants = [str(x).strip().lower() for x in only_ids if str(x).strip()]
    if not wants:
        return services_by_type
    out: dict[str, Any] = {}
    for stype, data in services_by_type.items():
        if not isinstance(data, dict) or data.get("status") != "success":
            if isinstance(data, dict) and ("error" in data or data.get("status") == "error"):
                out[stype] = data
            continue
        instances = extract_service_instances(data)
        kept = []
        for inst in instances:
            name = instance_name(inst).lower()
            if any(w == name or w in name for w in wants):
                kept.append(inst)
        # Rebuild a success envelope with filtered list
        payload = dict(data)
        nested = payload.get("data")
        if isinstance(nested, dict):
            nested = dict(nested)
            nested["services"] = kept
            payload["data"] = nested
        else:
            payload = {
                "status": "success",
                "data": {"services": kept},
            }
        out[stype] = payload
    return out


async def collect_services_fact_slice(
    client: Any,
    settings: FactCollectSettings,
    *,
    only_service_types: Sequence[str] | None = None,
    only_service_ids: Sequence[str] | None = None,
    include_fleet_sync: bool = True,
) -> dict[str, Any]:
    """Service types, fleet sync, instances, health records, and counts.

    - ``only_service_types is None``: list types via MCP, then fetch each.
    - ``only_service_types = ["l3rt", ...]``: skip listing; fetch those only.
    - ``only_service_types = []``: skip all service MCP (device-focus / sync-only).
    """
    services_by_type: dict[str, Any] = {}
    sync_modules: dict[str, str] = {}

    if only_service_types is not None:
        type_names = [
            normalize_service_type(t)
            for t in only_service_types
            if str(t).strip()
        ]
        service_types: Any = {
            "status": "success",
            "data": {"service_types": list(type_names), "focused": True},
        }
    else:
        service_types = await call_mcp(client, "get_service_types")
        type_names = select_service_types(
            extract_service_type_names(service_types),
            settings.ignore_service_types,
        )

    if include_fleet_sync:
        fleet_sync = await call_mcp(client, "get_fleet_sync_summary")
    else:
        fleet_sync = {
            "status": "success",
            "data": {"devices": [], "summary": {}},
        }

    limit = settings.max_service_types
    for stype in type_names[:limit]:
        query_type = normalize_service_type(stype)
        sync_modules[query_type] = sync_module_from_type(stype)
        try:
            services_by_type[query_type] = await call_mcp(
                client, "get_services", {"service_type": query_type}
            )
        except Exception as exc:  # noqa: BLE001 — partial snapshot
            services_by_type[query_type] = {"error": str(exc)}

    if only_service_ids:
        services_by_type = _filter_services_payload_by_ids(
            services_by_type, only_service_ids
        )

    device_sync_map = build_device_sync_map(fleet_sync)
    mode = getattr(settings, "service_sync_mode", "check")
    if type_names:
        services = await collect_service_health(
            client,
            services_by_type,
            sync_modules,
            device_sync_map,
            service_sync_mode=mode,
        )
    else:
        services = {}
    from nso_facts.service_collect import (
        SERVICE_SYNC_MODE_SKIP,
        SERVICE_SYNC_SKIP_NOTE,
        normalize_service_sync_mode,
    )

    note: str | None = None
    if normalize_service_sync_mode(mode) == SERVICE_SYNC_MODE_SKIP and services:
        note = SERVICE_SYNC_SKIP_NOTE
    elif services:
        null_n = sum(1 for r in services.values() if r.get("in_sync") is None)
        if null_n and null_n == len(services):
            note = (
                f"Service sync returned null for {null_n}/{len(services)} "
                "instances; system status used endpoint fleet sync where "
                "available. Dataplane verification is separate."
            )
    return {
        "service_types": service_types,
        "ignored_service_types": sorted(settings.ignore_service_types),
        "fleet_sync": fleet_sync,
        "services_by_type": services_by_type,
        "services": services,
        "counts": derive_counts(services_by_type, services),
        "service_sync_note": note,
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
    include_fleet_sync: bool = True,
    physical_edges: list[dict[str, Any]] | None = None,
    include_physical_operational: bool = False,
    only_service_types: Sequence[str] | None = None,
    only_service_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Compose the shared non-LLM fact pack used by production and multi-agent.

    Does **not** build full topology (callers use ``load_or_build_topology`` or
    multi-agent ``assemble_topology``). Does **not** open an MCP session.
    """
    pack = await collect_services_fact_slice(
        client,
        settings,
        only_service_types=only_service_types,
        only_service_ids=only_service_ids,
        include_fleet_sync=include_fleet_sync,
    )

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
