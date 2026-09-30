"""Shared prerequisite evaluation using this run's collected facts."""
from datetime import datetime, timezone


def sync_ready(record):
    devices = record.get("devices") or []
    states = record.get("device_sync") or {}
    return (bool(devices) and record.get("in_sync") is not False
            and record.get("system_status") == "up"
            and all(states.get(d) == "in-sync" for d in devices))


def result(record, status, reason, checks):
    return {"schema_version": 1, "service_type": record.get("service_type"),
            "service_id": record.get("name"), "devices": list(record.get("devices") or []),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "sync_ready": sync_ready(record), "status": status, "reason": reason,
            "needs_investigation": status in {"down", "unknown"},
            "checks": checks, "customer_delivery_tested": False}
