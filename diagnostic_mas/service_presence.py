"""Fresh inventory reconciliation for a service reported missing during a dig."""
import re
from datetime import datetime, timezone
from nso_facts.mcp_client import call_mcp
from nso_facts.health import instance_name
from diagnostic_mas.case import add_evidence


def disappeared(record):
    return (record.get("presence_recheck") or {}).get("outcome") == "absent"


async def reconcile_presence(client, case, record, finding):
    # Trigger from a missing-object conclusion, not ordinary down links.
    if finding.get("dataplane_status") == "up":
        return finding
    text = str(finding.get("observed") or "") + " " + str(finding.get("cause") or "")
    if not re.search(r"not found|absent|no longer exists|de.?provision|decommission|deleted|missing.*(?:config|interface|bridge|service)", text, re.I):
        return finding
    stamp = datetime.now(timezone.utc).isoformat()
    outcome, reason = "unknown", "Fresh inventory query did not establish service presence"
    try:
        result = await call_mcp(client, "get_services", {"service_type":record["service_type"]}, bypass_cache=True)
        data = result.get("data") if isinstance(result, dict) else None
        rows = data.get("services") if isinstance(data, dict) else None
        # Never interpret malformed, partial, paginated or failed output as absence.
        valid = (isinstance(result, dict) and result.get("status") == "success"
                 and isinstance(rows, list) and all(isinstance(r, dict) and instance_name(r) for r in rows)
                 and not any(data.get(k) for k in ("partial", "truncated", "has_more", "next_cursor", "next")))
        if valid:
            outcome = "present" if any(instance_name(r) == record["name"] for r in rows) else "absent"
            reason = "Exact service ID " + ("present" if outcome == "present" else "absent") + " in fresh service-type inventory"
    except Exception as exc:
        reason = "Fresh inventory query failed: " + type(exc).__name__
    check = {"outcome":outcome, "checked_at":datetime.now(timezone.utc).isoformat(),
             "initial_observed_at":(record.get("basic_checks") or {}).get("checked_at"),
             "service_type":record.get("service_type"),"service_id":record.get("name"),"reason":reason}
    record["presence_recheck"] = check
    add_evidence(case, {"kind":"service_presence", "role":"service", "layer":"services", "payload":check})
    if outcome == "present":
        return finding
    out = dict(finding)
    out.update(dataplane_status="unknown", complete=False, fix_suggestion=None)
    if outcome == "absent":
        out["service_disappeared"] = True
        out["cause"] = "Service is no longer present in NSO: a fresh inventory taken during this scan no longer lists its exact ID."
        case.service_coverage[record["name"]] = "disappeared"
        for issue in case.issues:
            if issue.get("edge_id") == record["name"] and issue.get("layer") == "services":
                issue["status"] = "explained"
                issue["resolution"] = "Service no longer present in a fresh inventory; not a recovery."
    else:
        out["cause"] = reason + "; current service existence could not be verified."
    return out


def disappearance_section(case):
    from diagnostic_mas.device_health import services_from_case
    rows = [r for r in services_from_case(case).values() if disappeared(r)]
    if not rows:
        return []
    lines = ["## Services no longer present", "",
             f"{len(rows)} service(s) were no longer present in NSO when rechecked during this scan and are not counted in the service totals.", ""]
    for r in rows:
        p = r["presence_recheck"]
        lines.append(f"- `{r['service_type']}/{r['name']}` — listed {p.get('initial_observed_at') or 'during initial collection'}; no longer present at {p['checked_at']}.")
    return lines + [""]
