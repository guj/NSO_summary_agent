"""Shared topology graph helpers."""

from __future__ import annotations

from typing import Any


def interface_edge_id(device: str, interface: str) -> str:
    return f"if:{device}:{interface}"


def isis_edge_id(
    device_a: str,
    interface_a: str,
    device_b: str,
    interface_b: str,
) -> str:
    end_a = (device_a, interface_a)
    end_b = (device_b, interface_b)
    if end_a <= end_b:
        return f"isis:{device_a}:{interface_a}:{device_b}:{interface_b}"
    return f"isis:{device_b}:{interface_b}:{device_a}:{interface_a}"


def service_edge_id(
    service_type: str,
    name: str,
    device_a: str,
    device_b: str | None,
) -> str:
    if device_b is None:
        return f"svc:{service_type}:{name}:{device_a}:_local"
    a, b = sorted((device_a, device_b))
    return f"svc:{service_type}:{name}:{a}:{b}"


def bgp_edge_id(
    device_a: str,
    address_a: str,
    device_b: str,
    address_b: str,
) -> str:
    """Canonical BGP session id — addresses then devices, lexicographically ordered."""
    pair_a = (address_a, device_a)
    pair_b = (address_b, device_b)
    if pair_a <= pair_b:
        return f"bgp:{address_a}:{address_b}:{device_a}:{device_b}"
    return f"bgp:{address_b}:{address_a}:{device_b}:{device_a}"


def ordered_bgp_endpoints(
    device_a: str,
    address_a: str,
    device_b: str,
    address_b: str,
) -> tuple[tuple[str, str], tuple[str, str]]:
    pair_a = (address_a, device_a)
    pair_b = (address_b, device_b)
    if pair_a <= pair_b:
        return (device_a, address_a), (device_b, address_b)
    return (device_b, address_b), (device_a, address_a)


def ordered_isis_endpoints(
    device_a: str,
    interface_a: str,
    device_b: str,
    interface_b: str,
) -> tuple[tuple[str, str], tuple[str, str]]:
    end_a = (device_a, interface_a)
    end_b = (device_b, interface_b)
    if end_a <= end_b:
        return end_a, end_b
    return end_b, end_a


def empty_static_layers() -> dict[str, Any]:
    return {
        "physical": {"edges": [], "summary": {"total": 0, "by_device": {}}},
        "underlay": {"edges": [], "summary": {"total": 0, "by_device": {}}},
        "routing": {"edges": [], "summary": {"total": 0, "by_device": {}}},
        "services": {"edges": [], "summary": {"total": 0, "by_type": {}}},
    }


def empty_operational_layers() -> dict[str, Any]:
    return {
        "physical": {
            "edges": [],
            "summary": {
                "total": 0,
                "up": 0,
                "down": 0,
                "admin-down": 0,
                "degraded": 0,
                "unknown": 0,
                "by_device": {},
            },
        },        "underlay": {
            "edges": [],
            "summary": {
                "total": 0,
                "up": 0,
                "down": 0,
                "unidirectional": 0,
                "unknown": 0,
                "by_device": {},
            },
        },
        "routing": {
            "edges": [],
            "summary": {
                "total": 0,
                "up": 0,
                "down": 0,
                "degraded": 0,
                "unknown": 0,
                "by_device": {},
            },
        },
        "services": {
            "edges": [],
            "summary": {
                "total": 0,
                "up": 0,
                "down": 0,
                "degraded": 0,
                "unknown": 0,
            },
        },
    }


def summarize_static_physical(edges: list[dict[str, Any]]) -> dict[str, Any]:
    by_device = _count_edges_by_device(edges)
    return {"total": len(edges), "by_device": by_device}


def summarize_operational_physical(edges: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "total": len(edges),
        "up": 0,
        "down": 0,
        "admin-down": 0,
        "degraded": 0,
        "unknown": 0,
        "by_device": {},
    }
    by_device: dict[str, dict[str, int]] = {}
    empty_bucket = {
        "total": 0,
        "up": 0,
        "down": 0,
        "admin-down": 0,
        "degraded": 0,
        "unknown": 0,
    }

    for edge in edges:
        status = (edge.get("state") or {}).get("status", "unknown")
        if status in summary and isinstance(summary[status], int):
            summary[status] += 1
        else:
            summary["unknown"] += 1

        device = device_from_physical_edge(edge)
        if not device:
            continue
        bucket = by_device.setdefault(device, dict(empty_bucket))
        bucket["total"] += 1
        if status in bucket:
            bucket[status] += 1
        else:
            bucket["unknown"] += 1

    summary["by_device"] = dict(sorted(by_device.items()))
    return summary


def device_from_physical_edge(edge: dict[str, Any]) -> str | None:
    local = edge.get("local") or {}
    device = local.get("device")
    if isinstance(device, str) and device.strip():
        return device.strip()
    edge_id = edge.get("id")
    if isinstance(edge_id, str):
        return device_from_edge_id(edge_id)
    return None


def device_from_edge_id(edge_id: str) -> str | None:
    if not edge_id.startswith("if:"):
        return None
    parts = edge_id.split(":", 2)
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return None


def _count_edges_by_device(edges: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for edge in edges:
        device = device_from_physical_edge(edge)
        if device:
            counts[device] = counts.get(device, 0) + 1
    return dict(sorted(counts.items()))


def summarize_static_underlay(edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {"total": len(edges), "by_device": _count_underlay_edges_by_device(edges)}


def summarize_operational_underlay(edges: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "total": len(edges),
        "up": 0,
        "down": 0,
        "unidirectional": 0,
        "unknown": 0,
        "by_device": {},
    }
    by_device: dict[str, dict[str, int]] = {}

    for edge in edges:
        status = (edge.get("state") or {}).get("status", "unknown")
        if status in summary and isinstance(summary[status], int):
            summary[status] += 1
        else:
            summary["unknown"] += 1

        for device in devices_from_underlay_edge(edge):
            bucket = by_device.setdefault(
                device,
                {
                    "total": 0,
                    "up": 0,
                    "down": 0,
                    "unidirectional": 0,
                    "unknown": 0,
                },
            )
            bucket["total"] += 1
            if status in bucket:
                bucket[status] += 1
            else:
                bucket["unknown"] += 1

    summary["by_device"] = dict(sorted(by_device.items()))
    return summary


def devices_from_underlay_edge(edge: dict[str, Any]) -> list[str]:
    devices: list[str] = []
    for end in ("local", "remote"):
        device = (edge.get(end) or {}).get("device")
        if isinstance(device, str) and device.strip():
            devices.append(device.strip())
    return devices


def _count_underlay_edges_by_device(edges: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for edge in edges:
        for device in devices_from_underlay_edge(edge):
            counts[device] = counts.get(device, 0) + 1
    return dict(sorted(counts.items()))


def summarize_static_routing(edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {"total": len(edges), "by_device": _count_routing_edges_by_device(edges)}


def summarize_static_services(edges: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    for edge in edges:
        meta = edge.get("meta") or {}
        st = str(meta.get("service_type") or "unknown")
        by_type[st] = by_type.get(st, 0) + 1
    return {"total": len(edges), "by_type": dict(sorted(by_type.items()))}


def summarize_operational_services(edges: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "total": len(edges),
        "up": 0,
        "down": 0,
        "degraded": 0,
        "unknown": 0,
    }
    for edge in edges:
        status = (edge.get("state") or {}).get("status", "unknown")
        if status in summary and isinstance(summary[status], int):
            summary[status] += 1
        else:
            summary["unknown"] += 1
    return summary


def summarize_operational_routing(edges: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "total": len(edges),
        "up": 0,
        "down": 0,
        "degraded": 0,
        "unknown": 0,
        "by_device": {},
    }
    by_device: dict[str, dict[str, int]] = {}

    for edge in edges:
        status = (edge.get("state") or {}).get("status", "unknown")
        if status in summary and isinstance(summary[status], int):
            summary[status] += 1
        else:
            summary["unknown"] += 1

        for device in devices_from_routing_edge(edge):
            bucket = by_device.setdefault(
                device,
                {
                    "total": 0,
                    "up": 0,
                    "down": 0,
                    "degraded": 0,
                    "unknown": 0,
                },
            )
            bucket["total"] += 1
            if status in bucket:
                bucket[status] += 1
            else:
                bucket["unknown"] += 1

    summary["by_device"] = dict(sorted(by_device.items()))
    return summary


def devices_from_routing_edge(edge: dict[str, Any]) -> list[str]:
    devices: list[str] = []
    for end in ("local", "remote"):
        device = (edge.get(end) or {}).get("device")
        if isinstance(device, str) and device.strip():
            devices.append(device.strip())
    return devices


def _count_routing_edges_by_device(edges: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for edge in edges:
        for device in devices_from_routing_edge(edge):
            counts[device] = counts.get(device, 0) + 1
    return dict(sorted(counts.items()))


def summarize_issues(issues: list[dict[str, Any]]) -> dict[str, Any]:
    by_layer: dict[str, int] = {
        "physical": 0,
        "underlay": 0,
        "routing": 0,
        "services": 0,
    }
    for issue in issues:
        layer = issue.get("layer")
        if layer in by_layer:
            by_layer[layer] += 1
    return {"issues_total": len(issues), "issues_by_layer": by_layer}
