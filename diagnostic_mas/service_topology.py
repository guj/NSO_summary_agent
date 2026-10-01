"""Offline service membership view. No new collection or inferred physical paths."""
from collections import Counter
import html
import json
from pathlib import Path


def service_topology_data(case):
    if not case:
        return []
    services = {}
    for ev in case.get("evidence", []):
        if ev.get("kind") == "spine" and ev.get("role") == "service":
            services.update((ev.get("payload", {}).get("extra") or {}).get("services") or {})
    counts = Counter(str(r.get("name") or k.split("/", 1)[-1]) for k, r in services.items())
    digs = {}
    for d in case.get("diagnoses", []):
        if d.get("kind") == "dataplane":
            subject = d.get("subject") or {}
            name = subject.get("name") or d.get("name")
            kind = subject.get("service_type")
            if kind or counts[name] == 1:
                digs[kind, name] = d
    try:
        from diagnostic_mas.service_final_status import assessment
    except ModuleNotFoundError:
        # Older runners have no final-status helper. Do not promote legacy sync-up
        # into operational readiness; preserve explicitly recorded DP results only.
        def assessment(record, dig):
            status = record.get("dataplane_status", "unknown")
            if status not in {"up", "down", "degraded"}:
                status = "unknown"
            return status, "Legacy saved dataplane status; consult report findings"
    result = []
    for key, record in services.items():
        if (record.get("presence_recheck") or {}).get("outcome") == "absent":
            continue
        kind = str(record.get("service_type") or key.split("/", 1)[0])
        name = str(record.get("name") or key.split("/", 1)[-1])
        dig = digs.get((kind, name), digs.get((None, name)))
        status, basis = assessment(record, dig)
        basic = record.get("basic_checks") or {}
        checks = basic.get("checks") or []
        devices = set(record.get("devices") or [])
        attachments = []
        for check in checks:
            obs = check.get("observation")
            device = check.get("device") or (obs.get("device") if isinstance(obs, dict) else None)
            if device:
                devices.add(device)
            if device and check.get("check") in {"attachment", "gateway", "xconnect"}:
                label = obs.get("ac") if isinstance(obs, dict) else obs
                if label:
                    item = {"device": device, "label": str(label), "status": check.get("status", "unknown")}
                    if item not in attachments:
                        attachments.append(item)
        for endpoint in (record.get("live_l2") or {}).get("endpoints", []):
            device, ac = endpoint.get("device"), endpoint.get("ac")
            if device:
                devices.add(device)
            if device and ac and not any(a["device"] == device and a["label"] == ac for a in attachments):
                attachments.append({"device": device, "label": ac, "status": endpoint.get("ac_st") or "unknown"})
        intent = basic.get("intent") or {}
        external = intent.get("external-access") or {}
        borders = [x.get("device") for x in external.get("border-router", []) if isinstance(x, dict) and x.get("device")]
        devices.update(borders)
        result.append({"key": key, "name": name, "type": kind, "status": status, "basis": basis,
                       "devices": sorted(devices), "borders": borders, "attachments": attachments,
                       "sync": record.get("device_sync") or {}, "basic_status": basic.get("status", "not recorded"),
                       "basic_reason": basic.get("reason", ""), "checked_at": basic.get("checked_at", ""),
                       "checks": [{k: c[k] for k in ("check", "device", "status", "observation", "command", "tool") if k in c} for c in checks],
                       "diagnosis": {k: dig[k] for k in ("source", "status", "complete", "observed", "cause", "fix_suggestion", "verification_gap", "evidence_ids") if k in dig} if dig else None})
    return sorted(result, key=lambda r: ({"down": 0, "degraded": 1, "unknown": 2, "up": 3}.get(r["status"], 2), r["type"], r["name"]))


def render_service_topology(case):
    data = service_topology_data(case)
    if not data:
        return ""
    encoded = json.dumps(data, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    document = Path(__file__).with_name("service_topology_template.html").read_text().replace("__SERVICE_DATA__", encoded)
    return ('<details class="section" id="service-topology"><summary>Service topology</summary>'
            '<p>Saved service membership and final assessment. Logical dependencies, not physical links. '
            'OpUp means PE-side readiness; customer delivery was not tested.</p>'
            '<iframe title="Interactive service topology" sandbox="allow-scripts" '
            'style="width:100%;height:850px;border:0;display:block" srcdoc="' + html.escape(document, quote=True) + '"></iframe>'
            '<noscript>Enable JavaScript for service selection; service findings remain in the report text.</noscript></details>')
