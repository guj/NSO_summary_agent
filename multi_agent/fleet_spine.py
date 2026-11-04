"""Fleet-level MCP collect for multi-agent executive sections."""

from __future__ import annotations

from typing import Any, Sequence

from agent.config import Settings
from nso_facts.fact_pack import build_fact_pack


async def collect_fleet_spine(
    client: Any,
    settings: Settings,
    device_names: list[str],
    *,
    physical_edges: list[dict[str, Any]],
    only_service_types: Sequence[str] | None = None,
    only_service_ids: Sequence[str] | None = None,
    lean: bool = False,
    retry_inconclusive_sync: bool = False,
    operational_policy: str = "",
    spine_concurrent_devices: int = 1,
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
        retry_inconclusive_sync=retry_inconclusive_sync,
        operational_policy=operational_policy,
        spine_concurrent_devices=spine_concurrent_devices,
        physical_edges=physical_edges,
        include_physical_operational=bool(physical_edges) and not lean,
        only_service_types=only_service_types,
        only_service_ids=only_service_ids,
    )
