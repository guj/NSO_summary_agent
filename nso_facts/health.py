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
    """Extract service sync boolean from MCP ``check_service_sync`` responses.

    MCP payloads vary by NED/tooling. Accept common keys under ``data`` and
    ``data.details``:

    - ``in_sync`` / ``in-sync`` (bool or string)
    - ``sync_state`` (e.g. ``"in-sync"`` / ``"out-of-sync"``) — do not ignore
    - ``result`` (same string vocabulary)

    Values are normalized by ``_coerce_sync_value`` (``in-sync`` and ``in_sync``
    both count as synced).
    """
    if not isinstance(sync_result, dict) or sync_result.get("status") != "success":
        return None

    data = sync_result.get("data")
    if not isinstance(data, dict):
        return None

    for key in ("in_sync", "in-sync", "sync_state", "result"):
        direct = _coerce_sync_value(data.get(key))
        if direct is not None:
            return direct

    details = data.get("details")
    if isinstance(details, dict):
        for key in ("in-sync", "in_sync", "sync_state", "result"):
            nested = _coerce_sync_value(details.get(key))
            if nested is not None:
                return nested

    return None


def classify_system_status(
    sync_result: dict[str, Any] | None,
    device_results: list[str | None],
) -> str:
    """NSO/device sync layer only: up, degraded, or unknown — never down.

    Status policy (service reporting):
    - **Down** requires positive evidence a required service path failed
      (AC/XC/segment down, missing required route, scoped traffic failure).
      That comes from dataplane/live evidence via ``apply_dataplane_status``,
      not from this sync classifier.
    - **Unknown** when queries time out, tools error, or ``in_sync`` is None —
      a query failure describes the investigation, not the service.
    - **Degraded** for configuration drift (out-of-sync), not forwarding down.
      In-sync does not prove forwarding works.
    """
    # Any sync/device query failure → unknown (incl. unreachable / timeout).
    if any(_is_verification_gap(result) for result in device_results):
        return "unknown"

    if sync_result and sync_result.get("status") == "error":
        # Service sync MCP error: could not verify — not confirmed path failure.
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

    # in_sync is None / missing and no usable device results → unknown
    return "unknown"


def classify_dataplane_status(live_l2: dict[str, Any] | None) -> str:
    """Collector never sets dataplane from live_l2 — always not_checked.

    Use ``apply_dataplane_status`` after the LLM verify phase.
    """
    del live_l2
    return "not_checked"


_STATUS_RANK = {
    "up": 0,
    "not_checked": 0,  # ignored when combining; kept for completeness
    "unknown": 1,
    "degraded": 2,
    "down": 3,
}


def combine_service_status(system_status: str, dataplane_status: str) -> str:
    """Overall status = worst of system and dataplane (not_checked ignored)."""
    layers: list[str] = []
    sys = str(system_status or "unknown").lower()
    dp = str(dataplane_status or "not_checked").lower()
    if sys in _STATUS_RANK and sys != "not_checked":
        layers.append(sys)
    if dp in _STATUS_RANK and dp != "not_checked":
        layers.append(dp)
    if not layers:
        return "unknown"
    return max(layers, key=lambda s: _STATUS_RANK.get(s, 1))


def classify_instance(
    sync_result: dict[str, Any] | None,
    device_results: list[str | None],
    *,
    live_l2_summary: str | None = None,
) -> str:
    """Overall status from system/sync only (dataplane is LLM, not hardcode).

    ``live_l2_summary`` is ignored for status (kept for call-compat).
    """
    del live_l2_summary  # dataplane not hardcoded into overall status
    return classify_system_status(sync_result, device_results)


def apply_dataplane_status(record: dict[str, Any], dataplane_status: str) -> None:
    """Set LLM dataplane result and recompute overall ``status`` in-place."""
    dp = str(dataplane_status or "not_checked").lower()
    if dp not in _STATUS_RANK:
        dp = "unknown"
    record["dataplane_status"] = dp
    sys = str(record.get("system_status") or "unknown").lower()
    record["status"] = combine_service_status(sys, dp)


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
    system_status = classify_system_status(sync_result, device_results)
    # Dataplane is LLM-only; collector never marks dataplane up/down.
    dataplane_status = "not_checked"
    status = combine_service_status(system_status, dataplane_status)
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
        "system_status": system_status,
        "dataplane_status": dataplane_status,
        "status": status,
    }
    if isinstance(live_l2, dict):
        # Evidence for LLM only — does not set dataplane_status
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


def _is_verification_gap(result: str | None) -> bool:
    """True for sync/device query failures that do not prove forwarding is down.

    Timeouts, tool/API errors, unreachable/connection-refused from NSO, and
    similar investigation failures → ``unknown``, never service ``down``.
    """
    if not result:
        return False
    lowered = result.lower()
    if lowered in {"in-sync", "out-of-sync", "out of sync", "not-in-sync"}:
        return False
    if lowered.startswith("error"):
        return True
    return any(
        token in lowered
        for token in (
            "timed out",
            "timeout",
            "read timeout",
            "unreachable",
            "connection refused",
        )
    )


def _is_confirmed_unreachable(message: str | None) -> bool:
    """Device/NED unreachability wording (still a query failure for services)."""
    if not message:
        return False
    lowered = message.lower()
    return "unreachable" in lowered or "connection refused" in lowered


def _is_device_down(result: str | None) -> bool:
    """Deprecated alias — sync-layer 'device down' is a verification gap."""
    return _is_verification_gap(result)


def _looks_unreachable(message: str) -> bool:
    """True for timeout/API/unreachable query failures."""
    return _is_verification_gap(message)
