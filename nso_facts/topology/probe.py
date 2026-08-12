"""Debug topology layer collection against NSO MCP."""

from __future__ import annotations

import json
from typing import Any

from nso_facts.mcp_client import call_mcp
from nso_facts.topology.devices import parse_device_names
from nso_facts.topology.physical import (
    collect_operational_physical,
    collect_static_physical,
    probe_static_interface_discovery,
)
from nso_facts.topology.underlay import (
    collect_operational_underlay,
    collect_static_underlay,
    probe_static_isis_discovery,
)
from nso_facts.topology.routing import (
    collect_operational_routing,
    collect_static_routing,
    probe_static_bgp_discovery,
)


async def probe_physical_layer(
    client: Any,
    *,
    device: str | None = None,
) -> dict[str, Any]:
    """Run static discovery probes and optional operational refresh for one/all devices."""
    devices = parse_device_names(await call_mcp(client, "list_devices"))
    if device:
        if device not in devices:
            return {
                "error": f"device {device!r} not in NSO inventory",
                "devices": devices,
            }
        targets = [device]
    else:
        targets = devices

    report: dict[str, Any] = {"devices": devices, "probes": {}}
    for dev in targets:
        report["probes"][dev] = await probe_static_interface_discovery(client, dev)

    static_edges, static_issues, static_cov = await collect_static_physical(
        client, targets if device else devices
    )
    report["static"] = {
        "coverage": static_cov,
        "edge_count": len(static_edges),
        "by_device": _by_device(static_edges),
        "edges": static_edges,
        "issues": static_issues,
    }

    if static_edges:
        op_edges, op_issues, op_cov = await collect_operational_physical(
            client, static_edges
        )
        report["operational"] = {
            "coverage": op_cov,
            "edge_count": len(op_edges),
            "issues": op_issues,
        }

    return report


async def probe_underlay_layer(
    client: Any,
    *,
    device: str | None = None,
) -> dict[str, Any]:
    devices = parse_device_names(await call_mcp(client, "list_devices"))
    if device:
        if device not in devices:
            return {
                "error": f"device {device!r} not in NSO inventory",
                "devices": devices,
            }
        targets = [device]
    else:
        targets = devices

    report: dict[str, Any] = {"devices": devices, "probes": {}}
    for dev in targets:
        report["probes"][dev] = await probe_static_isis_discovery(client, dev)

    static_edges, static_issues, static_cov = await collect_static_underlay(
        client, targets if device else devices
    )
    report["static"] = {
        "coverage": static_cov,
        "edge_count": len(static_edges),
        "edges": static_edges,
        "issues": static_issues,
    }

    op_edges, op_issues, op_cov = await collect_operational_underlay(
        client, targets if device else devices, static_edges
    )
    report["operational"] = {
        "coverage": op_cov,
        "edge_count": len(op_edges),
        "issues": op_issues,
    }
    return report


async def probe_routing_layer(
    client: Any,
    *,
    device: str | None = None,
) -> dict[str, Any]:
    devices = parse_device_names(await call_mcp(client, "list_devices"))
    if device:
        if device not in devices:
            return {
                "error": f"device {device!r} not in NSO inventory",
                "devices": devices,
            }
        targets = [device]
    else:
        targets = devices

    report: dict[str, Any] = {"devices": devices, "probes": {}}
    for dev in targets:
        report["probes"][dev] = await probe_static_bgp_discovery(client, dev)

    static_edges, static_issues, static_cov = await collect_static_routing(
        client, targets if device else devices
    )
    report["static"] = {
        "coverage": static_cov,
        "edge_count": len(static_edges),
        "edges": static_edges,
        "issues": static_issues,
    }

    op_edges, op_issues, op_cov = await collect_operational_routing(
        client, targets if device else devices, static_edges
    )
    report["operational"] = {
        "coverage": op_cov,
        "edge_count": len(op_edges),
        "issues": op_issues,
    }
    return report


def _by_device(edges: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for edge in edges:
        dev = (edge.get("local") or {}).get("device")
        if dev:
            counts[dev] = counts.get(dev, 0) + 1
    return dict(sorted(counts.items()))


def format_probe_report(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, default=str)
