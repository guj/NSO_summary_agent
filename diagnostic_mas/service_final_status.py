"""Final assessment and exclusive provenance buckets for operator summaries."""
import json
from collections import Counter
from diagnostic_mas.device_health import services_from_case

STATUSES = ("up", "down", "degraded", "unknown")
LABELS = ("OpUp", "Down", "Degraded", "Unknown")
MARKER = "service-final-breakdown:"


def sync_bucket(service):
    devices = service.get("devices") or []
    states = [str((service.get("device_sync") or {}).get(d) or "").lower().replace("_", "-") for d in devices]
    if service.get("in_sync") is False or "out-of-sync" in states:
        return "Sync out"
    if not states or any(s != "in-sync" for s in states):
        return "Sync unknown"
    if (service.get("basic_checks") or {}).get("sync_ready") is False:
        return "Sync unknown"
    return None


def assessment(service, dig=None):
    stop = sync_bucket(service)
    if stop:
        return "unknown", stop
    basic = service.get("basic_checks") or {}
    operational = basic.get("status", service.get("operational_status", "unknown"))
    if operational not in STATUSES:
        operational = "unknown"
    if dig:
        status = str(dig.get("status") or "").lower()
        if status == "ok":
            status = "up"
        if dig.get("source") == "llm" and dig.get("complete") is not False and status in {"up", "down", "degraded"}:
            return status, "LLM concluded " + dict(zip(STATUSES, LABELS))[status]
        if dig.get("source") == "port_investigation" and status == "down":
            return status, "Down — port investigated once"
        if dig.get("source") == "device_investigation" and status == "down":
            return status, "Down — device investigated once"
    label = dict(zip(STATUSES, LABELS))[operational]
    if operational == "up" and not dig:
        return operational, "Basic operational pass"
    suffix = "LLM incomplete; retained" if dig else "not investigated"
    if operational == "unknown" and basic.get("coverage") == "interfaces_only" and not dig:
        states = [c.get("status") for c in basic.get("checks") or [] if c.get("check") == "interface"]
        seen = "Interfaces up" if states and all(s == "pass" for s in states) else "Interface state incomplete"
        return "unknown", seen + " — service logic not checked"
    if not basic and not service.get("operational_status"):
        return "unknown", "Operational evidence unavailable — " + suffix
    return operational, f"Operational {label} — {suffix}"


def final_service_assessments(case):
    """Yield (service_type, name, service, final status, reason) for each present service."""
    services = services_from_case(case)
    names = Counter(str(r.get("name") or k.split("/", 1)[-1]) for k,r in services.items())
    digs = {}
    for dig in case.diagnoses:
        if dig.get("kind") != "dataplane":
            continue
        subject = dig.get("subject") or {}
        name = subject.get("name") or dig.get("name")
        kind = subject.get("service_type")
        if kind or names.get(name) == 1:
            digs[kind, name] = dig
    from diagnostic_mas.service_presence import disappeared
    for key, service in services.items():
        if disappeared(service):
            continue
        kind = str(service.get("service_type") or key.split("/", 1)[0])
        name = str(service.get("name") or key.split("/", 1)[-1])
        dig = digs.get((kind,name), digs.get((None,name)))
        status, reason = assessment(service, dig)
        yield kind, name, service, status, reason


def final_service_counts(case):
    rows = {}
    for kind, _name, _service, status, reason in final_service_assessments(case):
        row = rows.setdefault(kind, {"total": 0, **{s: 0 for s in STATUSES}, "sources": {s: {} for s in STATUSES}})
        row["total"] += 1
        row[status] += 1
        sources = row["sources"][status]
        sources[reason] = sources.get(reason, 0) + 1
    return dict(sorted(rows.items()))


def format_final_services_table(rows):
    if not rows:
        return ["(none)"]
    lines = ["| Service type | Total | OpUp | Down | Degraded | Unknown |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for kind, row in rows.items():
        safe_kind = kind.replace("|", "&#124;").replace("\n", " ")
        lines.append("| " + safe_kind + " | " + " | ".join(str(row[k]) for k in ("total", *STATUSES)) + " |")
    # Hidden Markdown metadata supplies cell details to our HTML renderer.
    # Escape angle brackets so an untrusted service type cannot close the comment.
    payload = json.dumps(rows, ensure_ascii=True).replace("<", r"\u003c").replace(">", r"\u003e")
    lines += ["", "<!-- " + MARKER + payload + " -->", "",
              "Final status uses the latest supported assessment. Incomplete LLM digs retain the operational result. Sync out/unknown has final status Unknown. OpUp means PE-side readiness; customer traffic delivery was not tested.",
              "In HTML, expand a status count to see its contributing paths."]
    return lines
