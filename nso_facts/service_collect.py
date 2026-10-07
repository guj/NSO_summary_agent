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

SERVICE_SYNC_MODE_CHECK = "check"
SERVICE_SYNC_MODE_SKIP = "skip"
SERVICE_SYNC_SKIP_NOTE = (
    "SystemUp is a baseline from endpoint fleet sync (service sync "
    "skipped) — not fleet-wide dataplane verification. No dig or an "
    "incomplete dig keeps SystemUp; dig-confirmed down or degraded demotes."
)


def normalize_service_sync_mode(value: str | None) -> str:
    """Return ``check`` (default) or ``skip``."""
    text = (value or "").strip().lower()
    if text == SERVICE_SYNC_MODE_SKIP:
        return SERVICE_SYNC_MODE_SKIP
    return SERVICE_SYNC_MODE_CHECK


async def collect_service_health(
    client: Any,
    services_by_type: dict[str, Any],
    sync_modules: dict[str, str],
    device_sync_map: dict[str, str],
    *,
    service_sync_mode: str | None = None,
    operational_policy: str = "",
    spine_concurrent_devices: int = 1,
) -> dict[str, dict[str, Any]]:
    """Run check_service_sync (+ live L2 xconnect for l2ptp/l2sts) per instance.

    ``service_sync_mode=skip``: do not call ``check_service_sync``; classify
    system status from endpoint fleet sync only (for pockets where service
    sync always returns null). Dataplane is unchanged (still not_checked here).
    """
    mode = normalize_service_sync_mode(service_sync_mode)
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
            if is_l2_service_type(service_type) and not operational_policy:
                for ep in extract_l2_access_endpoints(instance):
                    l2_devices.add(ep["device"])

    rows_by_device = await _probe_l2vpn_xconnect(client, sorted(l2_devices))

    services: dict[str, dict[str, Any]] = {}
    for service_type, instance, sync_module, name in planned:
        key = f"{service_type}/{name}"
        if mode == SERVICE_SYNC_MODE_SKIP:
            sync_result = None
        else:
            try:
                sync_result = await call_mcp(
                    client,
                    "check_service_sync",
                    {"service_type": sync_module, "service_name": name},
                )
            except Exception as exc:  # noqa: BLE001 — keep collecting others
                sync_result = {"status": "error", "error_message": str(exc)}

        live_l2 = None
        if is_l2_service_type(service_type) and not operational_policy:
            endpoints = extract_l2_access_endpoints(instance)
            live_l2 = summarize_live_l2(endpoints, rows_by_device)

        record = build_instance_record(
            service_type,
            instance,
            sync_result,
            device_sync_map,
            live_l2=live_l2,
        )
        if mode == SERVICE_SYNC_MODE_SKIP:
            record["service_sync_mode"] = SERVICE_SYNC_MODE_SKIP
            record["system_status_basis"] = "endpoint_fleet_sync"
        else:
            record["service_sync_mode"] = SERVICE_SYNC_MODE_CHECK
        services[key] = record

    if operational_policy:
        from diagnostic_mas.operational_checks.common import sync_ready
        from diagnostic_mas.operational_checks.l2ptp import evaluate
        from diagnostic_mas.operational_checks.scheduler import DeviceCalls, ProbeCache, run_groups
        workers = spine_concurrent_devices
        calls = DeviceCalls(call_mcp, workers)
        force = operational_policy == "force"
        ptp = [(services[f"{t}/{n}"], inst) for t, inst, _, n in planned if t == "l2ptp"]
        eligible = [(r, inst) for r, inst in ptp if force or sync_ready(r)]
        needed = {ep["device"] for _, inst in eligible for ep in extract_l2_access_endpoints(inst)}
        # Reuse any observations already collected for other L2 services.
        async def probe_xconnect(device):
            rows_by_device.update(await _probe_l2vpn_xconnect(client, [device], call=calls))
        await run_groups(sorted(needed - l2_devices), probe_xconnect, lambda d: d, workers)
        for rec, inst in ptp:
            if force or sync_ready(rec):
                rec["live_l2"] = summarize_live_l2(extract_l2_access_endpoints(inst), rows_by_device)
            basic = evaluate(rec, force=force)
            rec["basic_checks"] = basic
            rec["operational_status"] = basic["status"]
            if basic["status"] in {"down", "unknown"}:
                rec["status"] = basic["status"]
        from diagnostic_mas.operational_checks.runner import evaluate as evaluate_other
        cache = ProbeCache()
        # Every other type: its own module, or the interface-only check when it has none.
        jobs = [(services[f"{t}/{n}"], inst) for t, inst, _, n in planned if t != "l2ptp"]
        async def evaluate_job(job):
            rec, instance = job
            basic = await evaluate_other(rec, instance, client, calls, cache, force=force)
            rec["basic_checks"] = basic
            rec["operational_status"] = basic["status"]
            if basic["status"] in {"down", "unknown", "degraded"}:
                rec["status"] = basic["status"]
        await run_groups(jobs, evaluate_job,
                         lambda job: next(iter(job[0].get("devices") or []), "unknown"), workers)
    return services


async def _probe_l2vpn_xconnect(
    client: Any,
    devices: list[str],
    *, call=None,
) -> dict[str, list[dict[str, Any]]]:
    """One ``show l2vpn xconnect`` per device; omit device on failure."""
    out: dict[str, list[dict[str, Any]]] = {}
    call = call or call_mcp
    for device in devices:
        try:
            result = await call(
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
