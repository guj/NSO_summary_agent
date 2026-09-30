"""L2PTP basic readiness: exact AC-matched xconnect on every endpoint."""
from .common import result, sync_ready


def evaluate(record, *, force=False):
    if not sync_ready(record) and not force:
        return result(record, "not_checked", "Sync prerequisite not satisfied", [])
    endpoints = (record.get("live_l2") or {}).get("endpoints") or []
    checks = [{"check": "xconnect", "tool": "exec_show",
               "command": "l2vpn xconnect", "observation": dict(ep),
               "status": ("unknown" if ep.get("error") else
                          "pass" if ep.get("st") == "UP" and ep.get("xconnect")
                          and all(ep.get(k) in (None, "", "UP") for k in ("ac_st", "seg2_st")) else
                          "fault" if ep.get("st") in {"DN", "AD", "UR"} or
                          any(ep.get(k) in {"DN", "AD", "UR"} for k in ("ac_st", "seg2_st")) else "unknown")}
              for ep in endpoints]
    if any(c["status"] == "fault" for c in checks):
        return result(record, "down", "Required endpoint xconnect is not operational", checks)
    devices = set(record.get("devices") or [])
    covered = {ep.get("device") for ep in endpoints if ep.get("ac")}
    if len(endpoints) >= 2 and devices and devices == covered and all(c["status"] == "pass" for c in checks):
        return result(record, "up", "All service endpoint xconnects are UP", checks)
    return result(record, "unknown", "Endpoint identity or xconnect evidence incomplete", checks)
