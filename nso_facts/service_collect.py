"""Service-type listing and per-instance sync health collect (MCP)."""

from __future__ import annotations

from typing import Any

from nso_facts.health import (
    build_instance_record,
    extract_service_instances,
    instance_name,
    sync_module_from_type,
)
from nso_facts.l2vpn_xconnect import (
    extract_l2_access_endpoints,
    is_l2_service_type,
    parse_l2vpn_xconnect,
    summarize_live_l2,
)
from nso_facts.mcp_client import call_mcp, mcp_data, mcp_is_error, unwrap_mcp_data


async def collect_service_health(
    client: Any,
    services_by_type: dict[str, Any],
    sync_modules: dict[str, str],
    device_sync_map: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Run check_service_sync (+ live L2 xconnect for l2ptp/l2sts) per instance."""
    planned: list[tuple[str, dict[str, Any], str, str]] = []
    l2_devices: set[str] = set()

    for service_type, data in services_by_type.items():
        if isinstance(data, dict) and (
            "error" in data or data.get("status") == "error"
        ):
            continue

        sync_module = sync_modules.get(service_type, service_type)
        for instance in extract_service_instances(data):
            name = instance_name(instance)
            if not name:
                continue
            planned.append((service_type, instance, sync_module, name))
            if is_l2_service_type(service_type):
                for ep in extract_l2_access_endpoints(instance):
                    l2_devices.add(ep["device"])

    rows_by_device = await _probe_l2vpn_xconnect(client, sorted(l2_devices))

    services: dict[str, dict[str, Any]] = {}
    for service_type, instance, sync_module, name in planned:
        key = f"{service_type}/{name}"
        try:
            sync_result = await call_mcp(
                client,
                "check_service_sync",
                {"service_type": sync_module, "service_name": name},
            )
        except Exception as exc:  # noqa: BLE001 — keep collecting others
            sync_result = {"status": "error", "error_message": str(exc)}

        live_l2 = None
        if is_l2_service_type(service_type):
            endpoints = extract_l2_access_endpoints(instance)
            live_l2 = summarize_live_l2(endpoints, rows_by_device)

        services[key] = build_instance_record(
            service_type,
            instance,
            sync_result,
            device_sync_map,
            live_l2=live_l2,
        )

    return services


async def _probe_l2vpn_xconnect(
    client: Any,
    devices: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """One ``show l2vpn xconnect`` per device; omit device on failure."""
    out: dict[str, list[dict[str, Any]]] = {}
    for device in devices:
        try:
            result = await call_mcp(
                client,
                "exec_show",
                {"device_name": device, "input_command": "l2vpn xconnect"},
            )
        except Exception:  # noqa: BLE001
            continue
        if mcp_is_error(result):
            continue
        text = _exec_show_text(result)
        if not text:
            continue
        out[device] = parse_l2vpn_xconnect(text)
    return out


def _exec_show_text(result: Any) -> str:
    data = unwrap_mcp_data(result)
    if isinstance(data, dict):
        return str(data.get("result") or "")
    return ""


def select_service_types(
    type_names: list[str],
    ignored: frozenset[str],
) -> list[str]:
    if not ignored:
        return type_names
    return [name for name in type_names if not is_ignored_service_type(name, ignored)]


def is_ignored_service_type(raw: str, ignored: frozenset[str]) -> bool:
    query_type = normalize_service_type(raw).lower()
    module = sync_module_from_type(raw).lower()
    return query_type in ignored or module in ignored


def extract_service_type_names(service_types: Any) -> list[str]:
    """Parse get_service_types response into raw type name strings.

    Accepts the MCP envelope ``{status, data:{service_types:[...]}}`` and
    legacy raw YANG (``tailf-ncs:service-type`` at the top level).
    """
    if mcp_is_error(service_types):
        return []

    payload: Any = service_types
    if isinstance(service_types, dict) and service_types.get("status") == "success":
        payload = mcp_data(service_types)

    if isinstance(payload, dict):
        for key in (
            "service_types",
            "tailf-ncs:service-type",
            "service-type",
            "service_type",
            "types",
        ):
            if key in payload:
                raw = payload[key]
                if isinstance(raw, list):
                    return [_name_from_entry(x) for x in raw if _name_from_entry(x)]
        services = payload.get("tailf-ncs:services", payload)
        if isinstance(services, dict):
            st = services.get("service-type", [])
            if isinstance(st, list):
                return [_name_from_entry(x) for x in st if _name_from_entry(x)]
    if isinstance(payload, list):
        return [_name_from_entry(x) for x in payload if _name_from_entry(x)]
    return []


def normalize_service_type(raw: str) -> str:
    """Extract service name from `/ncs:services/<module>:<service>` paths.

    Examples:
        /ncs:services/port-mirror:port-mirror  →  port-mirror
        /ncs:services/esnet:l2vpn-eline          →  l2vpn-eline
    """
    s = raw.strip().lstrip("/")
    for prefix in ("ncs:services/", "tailf-ncs:services/"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    if ":" in s:
        return s.split(":", 1)[1]
    return s


def _name_from_entry(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("name") or entry.get("id") or entry.get("type") or "")
    return ""


# Backward-compatible aliases used by older call sites / shims
_collect_service_health = collect_service_health
_select_service_types = select_service_types
_extract_service_type_names = extract_service_type_names
_is_ignored_service_type = is_ignored_service_type
