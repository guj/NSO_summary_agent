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


def checks_for_llm(basic):
    """A service's basic checks as the model is given them: each verdict with the commands it was read from,
    without the replies and the "looked for" / "found" sentences kept for the report. The model reads the device itself."""
    if not isinstance(basic, dict) or not isinstance(basic.get("checks"), list):
        return basic
    checks = []
    for check in basic.get("checks") or []:
        check = {key: value for key, value in check.items() if key not in ("looked_for", "found")}
        if check.get("sources"):
            check["sources"] = [{key: value for key, value in source.items() if key not in ("excerpt", "truncated")}
                                for source in check["sources"]]
        checks.append(check)
    return {**basic, "checks": checks}
