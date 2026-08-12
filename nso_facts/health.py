"""Per-service instance health classification."""

from __future__ import annotations

from typing import Any


def build_device_sync_map(fleet_sync: Any) -> dict[str, str]:
    if not isinstance(fleet_sync, dict) or fleet_sync.get("status") != "success":
        return {}
    devices = fleet_sync.get("data", {}).get("devices", [])
    if not isinstance(devices, list):
        return {}
    return {
        str(row["device"]): str(row["result"])
        for row in devices
        if isinstance(row, dict) and row.get("device") is not None
    }


def extract_service_instances(services_data: Any) -> list[dict[str, Any]]:
    if not isinstance(services_data, dict) or services_data.get("status") != "success":
        return []
    services = services_data.get("data", {}).get("services")
    if not isinstance(services, list):
        return []
    return [item for item in services if isinstance(item, dict)]


def sync_module_from_type(raw: str) -> str:
    """Module name for check_service_sync from `/ncs:services/<module>:<service>`."""
    s = raw.strip().lstrip("/")
    for prefix in ("ncs:services/", "tailf-ncs:services/"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    if ":" in s:
        return s.split(":", 1)[0]
    return s


def instance_name(instance: dict[str, Any]) -> str:
    for key in ("name", "id", "service-name", "service_name"):
        value = instance.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def extract_devices(instance: dict[str, Any]) -> list[str]:
    devices: list[str] = []
    _collect_device_leaves(instance, devices)

    seen: set[str] = set()
    unique: list[str] = []
    for name in devices:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def _collect_device_leaves(obj: Any, devices: list[str]) -> None:
    """Collect leafref device names from nested service YANG (e.g. l2ptp stp-a/z)."""
    if isinstance(obj, dict):
        device = obj.get("device")
        if isinstance(device, str) and device:
            devices.append(device)
        for value in obj.values():
            if isinstance(value, (dict, list)):
                _collect_device_leaves(value, devices)
    elif isinstance(obj, list):
        for item in obj:
            _collect_device_leaves(item, devices)


def parse_in_sync(sync_result: dict[str, Any] | None) -> bool | None:
    """Extract service sync boolean from MCP check_service_sync responses."""
    if not isinstance(sync_result, dict) or sync_result.get("status") != "success":
        return None

    data = sync_result.get("data")
    if not isinstance(data, dict):
        return None

    direct = _coerce_sync_value(data.get("in_sync"))
    if direct is not None:
        return direct

    details = data.get("details")
    if isinstance(details, dict):
        nested = _coerce_sync_value(details.get("in-sync"))
        if nested is not None:
            return nested
        nested = _coerce_sync_value(details.get("result"))
        if nested is not None:
            return nested

    return None


def classify_instance(
    sync_result: dict[str, Any] | None,
    device_results: list[str | None],
    *,
    live_l2_summary: str | None = None,
) -> str:
    """Return up, down, degraded, or unknown for one service instance."""
    for result in device_results:
        if _is_device_down(result):
            return "down"

    live = str(live_l2_summary or "").lower()
    if live == "down":
        return "down"
    if live == "degraded":
        return "degraded"

    if sync_result and sync_result.get("status") == "error":
        message = str(sync_result.get("error_message", "")).lower()
        if "not found" in message:
            return "unknown"
        if _looks_unreachable(message):
            return "down"
        return "unknown"

    in_sync = parse_in_sync(sync_result)

    if in_sync is False:
        return "degraded"

    for result in device_results:
        if result and result != "in-sync":
            return "degraded"

    if in_sync is True:
        return "up"

    known_devices = [result for result in device_results if result]
    if known_devices and all(result == "in-sync" for result in known_devices):
        return "up"

    return "unknown"


def build_instance_record(
    service_type: str,
    instance: dict[str, Any],
    sync_result: dict[str, Any] | None,
    device_sync_map: dict[str, str],
    *,
    live_l2: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name = instance_name(instance)
    devices = extract_devices(instance)
    device_results = [device_sync_map.get(device) for device in devices]
    live_summary = None
    if isinstance(live_l2, dict):
        live_summary = live_l2.get("summary")
    status = classify_instance(
        sync_result, device_results, live_l2_summary=str(live_summary)
        if live_summary is not None
        else None
    )
    in_sync = parse_in_sync(sync_result)

    record: dict[str, Any] = {
        "service_type": service_type,
        "name": name,
        "devices": devices,
        "in_sync": in_sync,
        "device_sync": {
            device: device_sync_map.get(device)
            for device in devices
        },
        "status": status,
    }
    if isinstance(live_l2, dict):
        record["live_l2"] = live_l2
    if sync_result and sync_result.get("status") == "error":
        record["sync_error"] = sync_result.get("error_message")
    elif status == "unknown" and isinstance(sync_result, dict):
        record["sync_raw"] = sync_result.get("data")
    return record


def derive_counts(
    services_by_type: dict[str, Any],
    services: dict[str, dict[str, Any]],
) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    by_type: dict[str, list[str]] = {}

    for key, record in services.items():
        service_type = str(record.get("service_type") or key.split("/", 1)[0])
        by_type.setdefault(service_type, []).append(record.get("status", "unknown"))

    for service_type, data in services_by_type.items():
        if isinstance(data, dict) and (
            "error" in data or data.get("status") == "error"
        ):
            counts[service_type] = {
                "total": 0,
                "up": 0,
                "down": 0,
                "degraded": 0,
                "unknown": 0,
            }
            continue

        statuses = by_type.get(service_type, [])
        bucket = {"total": 0, "up": 0, "down": 0, "degraded": 0, "unknown": 0}
        for status in statuses:
            bucket["total"] += 1
            field = status if status in bucket else "unknown"
            bucket[field] += 1
        counts[service_type] = bucket

    return counts


def _coerce_sync_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "in-sync", "sync"}:
            return True
        if lowered in {"false", "no", "out-of-sync", "out of sync", "not-in-sync"}:
            return False
    return None


def _is_device_down(result: str | None) -> bool:
    if not result:
        return False
    lowered = result.lower()
    return lowered.startswith("error") or "unreachable" in lowered


def _looks_unreachable(message: str) -> bool:
    return any(
        token in message
        for token in ("unreachable", "connection refused", "timed out", "timeout")
    )
