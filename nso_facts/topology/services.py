"""Layer 3 — services (endpoint pairs from collect service data)."""

from __future__ import annotations

from itertools import combinations
from typing import Any

from nso_facts.health import extract_devices, extract_service_instances, instance_name
from nso_facts.topology.graph import (
    service_edge_id,
    summarize_operational_services,
    summarize_static_services,
)

_STATUS_ISSUE = {
    "down": ("high", "service_down"),
    "degraded": ("medium", "service_degraded"),
    "unknown": ("low", "service_unknown"),
}


def build_static_service_edges(
    services_by_type: dict[str, Any],
    *,
    services: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    services = services or {}
    edges: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    for service_type, data in services_by_type.items():
        if isinstance(data, dict) and (
            "error" in data or data.get("status") == "error"
        ):
            continue
        for instance in extract_service_instances(data):
            name = instance_name(instance)
            if not name:
                continue
            key = f"{service_type}/{name}"
            record = services.get(key)
            if (
                isinstance(record, dict)
                and isinstance(record.get("devices"), list)
                and record["devices"]
            ):
                devices = [str(d) for d in record["devices"] if d]
            else:
                devices = extract_devices(instance)

            seen: set[str] = set()
            uniq: list[str] = []
            for d in devices:
                if d not in seen:
                    seen.add(d)
                    uniq.append(d)

            if not uniq:
                issues.append(
                    {
                        "severity": "low",
                        "layer": "services",
                        "code": "service_no_devices",
                        "edge_id": None,
                        "message": f"{service_type}/{name}: no devices on instance",
                    }
                )
                continue

            if len(uniq) == 1:
                dev = uniq[0]
                edges.append(
                    {
                        "id": service_edge_id(service_type, name, dev, None),
                        "type": "service_endpoint_pair",
                        "local": {"device": dev},
                        "remote": None,
                        "meta": {"service_type": service_type, "name": name},
                    }
                )
                continue

            for a, b in combinations(sorted(uniq), 2):
                edges.append(
                    {
                        "id": service_edge_id(service_type, name, a, b),
                        "type": "service_endpoint_pair",
                        "local": {"device": a},
                        "remote": {"device": b},
                        "meta": {"service_type": service_type, "name": name},
                    }
                )

    edges.sort(key=lambda e: e["id"])
    return edges, issues


def build_operational_service_edges(
    static_edges: list[dict[str, Any]],
    services: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    services = services or {}
    op_edges: list[dict[str, Any]] = []
    by_instance: dict[str, list[str]] = {}

    for edge in static_edges:
        meta = edge.get("meta") or {}
        st = str(meta.get("service_type") or "")
        name = str(meta.get("name") or "")
        key = f"{st}/{name}"
        eid = str(edge.get("id") or "")
        by_instance.setdefault(key, []).append(eid)
        record = services.get(key)
        status = "unknown"
        if isinstance(record, dict):
            status = str(record.get("status") or "unknown")
        op_edges.append({"id": eid, "state": {"status": status}})

    issues: list[dict[str, Any]] = []
    for key, edge_ids in sorted(by_instance.items()):
        record = services.get(key)
        status = "unknown"
        if isinstance(record, dict):
            status = str(record.get("status") or "unknown")
        if status == "up":
            continue
        mapping = _STATUS_ISSUE.get(status)
        if mapping is None:
            severity, code = "low", "service_unknown"
            status = "unknown"
        else:
            severity, code = mapping
        issues.append(
            {
                "severity": severity,
                "layer": "services",
                "code": code,
                "edge_id": sorted(edge_ids)[0] if edge_ids else None,
                "message": f"{key}: service status {status}",
            }
        )

    op_edges.sort(key=lambda e: e["id"])
    return op_edges, issues


def build_static_services_layer(edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": edges, "summary": summarize_static_services(edges)}


def build_operational_services_layer(edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": edges, "summary": summarize_operational_services(edges)}
