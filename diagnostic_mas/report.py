"""Deterministic report from case Evidence / Issues / Hypotheses."""

from __future__ import annotations

import re

from typing import Any

from diagnostic_mas.case import CaseFile
from diagnostic_mas.roles.summary import scrub_internal_ids
from multi_agent.topology_slice import issue_touches_device


def _issues_for_report(case: CaseFile) -> list[dict[str, Any]]:
    issues = list(case.issues)
    focus = list(case.focus_devices or [])
    if not focus:
        return issues
    return [
        i
        for i in issues
        if any(issue_touches_device(i, d) for d in focus)
        or any(
            d in (i.get("devices") or [])
            for d in focus
        )
    ]


def _display_key(ev: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(ev.get("kind") or "-"),
        str(ev.get("role") or "-"),
        str(ev.get("layer") or "-"),
    )


def _drilled_issue_keys(case: CaseFile) -> set[str]:
    """Issue ids / edge_ids that have a conclude drill_finding."""
    keys: set[str] = set()
    for ev in case.evidence:
        if ev.get("kind") != "drill_finding":
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        for field in ("issue_id", "issue_edge_id"):
            val = payload.get(field)
            if val is not None and str(val).strip():
                keys.add(str(val).strip())
    if not keys:
        return keys
    # Expand id ↔ edge_id so planner issue_ids match findings keyed by edge
    for issue in case.issues:
        iid = str(issue.get("id") or "").strip()
        edge = str(issue.get("edge_id") or "").strip()
        if iid and iid in keys and edge:
            keys.add(edge)
        if edge and edge in keys and iid:
            keys.add(iid)
    return keys


def _hypothesis_tied_to_drilled(hy: dict[str, Any], drilled: set[str]) -> bool:
    if not drilled:
        return False
    for iid in hy.get("issue_ids") or []:
        if str(iid).strip() in drilled:
            return True
    return False


def group_evidence_for_report(
    evidence: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], int]]:
    """Collapse Evidence that would print identically (kind/role/layer).

    Returns (representative_record, count) in first-seen order.
    """
    order: list[tuple[str, str, str]] = []
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for ev in evidence:
        key = _display_key(ev)
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(ev)
    out: list[tuple[dict[str, Any], int]] = []
    for key in order:
        items = groups[key]
        # Prefer a spine row with summaries when consolidating
        rep = items[0]
        for cand in items:
            payload = cand.get("payload") if isinstance(cand.get("payload"), dict) else {}
            if payload.get("operational_summary") or payload.get("static_summary"):
                rep = cand
                break
        out.append((rep, len(items)))
    return out


# Back-compat name used by tests
def dedupe_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [rep for rep, _count in group_evidence_for_report(evidence)]


def _service_counts_from_case(case: CaseFile) -> dict[str, dict[str, int]]:
    """Pull per-type up/down/degraded/unknown from service spine Evidence."""
    from diagnostic_mas.device_health import services_from_case
    from diagnostic_mas.focus import counts_from_services

    for ev in case.evidence:
        if ev.get("kind") != "spine" or ev.get("role") != "service":
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        op = payload.get("operational_summary")
        if isinstance(op, dict) and op:
            # counts: {stype: {up, down, ...}}
            if all(isinstance(v, dict) for v in op.values()):
                return {
                    str(k): {
                        "up": int(v.get("up") or 0),
                        "down": int(v.get("down") or 0),
                        "degraded": int(v.get("degraded") or 0),
                        "unknown": int(v.get("unknown") or 0),
                    }
                    for k, v in sorted(op.items())
                    if isinstance(v, dict)
                }
        extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        counts = extra.get("counts") if isinstance(extra, dict) else None
        if isinstance(counts, dict) and counts:
            return {
                str(k): {
                    "up": int(v.get("up") or 0),
                    "down": int(v.get("down") or 0),
                    "degraded": int(v.get("degraded") or 0),
                    "unknown": int(v.get("unknown") or 0),
                }
                for k, v in sorted(counts.items())
                if isinstance(v, dict)
            }
        services = extra.get("services") if isinstance(extra, dict) else None
        if isinstance(services, dict) and services:
            return counts_from_services(services)
    # Last resort: any services map attached to the case.
    services = services_from_case(case)
    if services:
        return counts_from_services(services)
    return {}


# Display labels for combined service status (fleet sync + finished digs).
# Incomplete digs do not demote SystemUp; dig down/degraded do.
_SERVICE_TABLE_COLS = (
    ("total", "Total"),
    ("up", "SystemUp"),
    ("down", "down"),
    ("degraded", "degraded"),
    ("unknown", "unknown"),
)


def _service_bucket_total(bucket: dict[str, int]) -> int:
    """Prefer explicit total; else sum status columns."""
    if "total" in bucket and bucket.get("total") is not None:
        try:
            return int(bucket.get("total") or 0)
        except (TypeError, ValueError):
            pass
    return (
        int(bucket.get("up") or 0)
        + int(bucket.get("down") or 0)
        + int(bucket.get("degraded") or 0)
        + int(bucket.get("unknown") or 0)
    )


def format_services_table(counts: dict[str, dict[str, int]]) -> list[str]:
    """Markdown table: service type × Total / SystemUp / down / degraded / unknown."""
    if not counts:
        return ["(none)"]
    name_w = max(len("service"), max(len(n) for n in counts))
    keys = [k for k, _label in _SERVICE_TABLE_COLS]
    labels = {k: label for k, label in _SERVICE_TABLE_COLS}
    enriched = {
        name: {**dict(bucket), "total": _service_bucket_total(bucket)}
        for name, bucket in counts.items()
    }
    col_w = {c: max(len(labels[c]), 3) for c in keys}
    for bucket in enriched.values():
        for c in keys:
            col_w[c] = max(col_w[c], len(str(bucket.get(c, 0))))

    def fmt_row(name: str, bucket: dict[str, int]) -> str:
        cells = [f"{name:<{name_w}}"] + [
            f"{str(bucket.get(c, 0)):>{col_w[c]}}" for c in keys
        ]
        return "| " + " | ".join(cells) + " |"

    header = (
        "| "
        + f"{'service':<{name_w}}"
        + " | "
        + " | ".join(f"{labels[c]:>{col_w[c]}}" for c in keys)
        + " |"
    )
    sep = (
        "| "
        + "-" * name_w
        + " | "
        + " | ".join("-" * col_w[c] for c in keys)
        + " |"
    )
    lines = [header, sep]
    for name, bucket in enriched.items():
        lines.append(fmt_row(name, bucket))
    return lines


def _dataplane_dig_subjects(case: CaseFile) -> dict[str, dict[str, Any]]:
    """Per-service dig outcome; prefer Diagnoses over Evidence."""
    rows: dict[str, dict[str, Any]] = {}
    for dx in case.diagnoses:
        if dx.get("kind") != "dataplane":
            continue
        subject = dx.get("subject") if isinstance(dx.get("subject"), dict) else {}
        name = str(subject.get("name") or dx.get("name") or "").strip()
        if not name:
            continue
        rows[name] = {
            "status": str(dx.get("status") or dx.get("dataplane_status") or "").lower(),
            "complete": dx.get("complete"),
        }
    for ev in case.evidence:
        kind = ev.get("kind")
        if kind not in {"dataplane_finding", "dataplane_incomplete"}:
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        name = str(payload.get("name") or "").strip()
        if not name or name in rows:
            continue
        complete = payload.get("complete")
        if complete is None:
            complete = kind != "dataplane_incomplete"
        rows[name] = {
            "status": str(payload.get("dataplane_status") or "").lower(),
            "complete": complete,
        }
    return rows


def _service_inventory_names(case: CaseFile) -> set[str]:
    """Known service instance names for not-checked tally."""
    names: set[str] = set()
    if case.service_coverage:
        names.update(str(n).strip() for n in case.service_coverage if str(n).strip())
    for ev in case.evidence:
        if ev.get("kind") != "spine" or ev.get("role") != "service":
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        services = extra.get("services") if isinstance(extra, dict) else None
        if isinstance(services, dict):
            for rec in _iter_service_records(services):
                name = str(rec.get("name") or "").strip()
                if name:
                    names.add(name)
    return names


def dataplane_dig_counts(case: CaseFile) -> dict[str, int]:
    """Tally dataplane dig outcomes: passed / inconclusive / not_checked."""
    digs = _dataplane_dig_subjects(case)
    passed = 0
    inconclusive = 0
    for row in digs.values():
        status = row.get("status") or ""
        complete = row.get("complete")
        if complete is False or status in {"unknown", "not_checked"}:
            inconclusive += 1
        elif status in {"up", "ok"}:
            passed += 1
        else:
            # Complete dig with a fault (down/degraded): not a clean pass.
            inconclusive += 1
    inventory = _service_inventory_names(case)
    if inventory:
        total = len(inventory)
    else:
        counts = _service_counts_from_case(case)
        total = sum(
            int(b.get("up") or 0)
            + int(b.get("down") or 0)
            + int(b.get("degraded") or 0)
            + int(b.get("unknown") or 0)
            for b in counts.values()
        ) if counts else len(digs)
    not_checked = max(0, total - len(digs))
    # Coverage may mark digs without a diagnosis row yet.
    for name, cov in (case.service_coverage or {}).items():
        if name in digs:
            continue
        if cov == "unresolved":
            inconclusive += 1
            not_checked = max(0, not_checked - 1)
        elif cov == "investigated":
            # Dig finished but diagnosis missing from case — treat as passed.
            passed += 1
            not_checked = max(0, not_checked - 1)
    return {
        "passed": passed,
        "inconclusive": inconclusive,
        "not_checked": not_checked,
    }


def format_dataplane_dig_line(case: CaseFile) -> str:
    """Coverage statement under the system-level services table."""
    digs = _dataplane_dig_subjects(case)
    passed = 0
    incomplete = 0
    faults = 0
    for row in digs.values():
        status = str(row.get("status") or "").lower()
        complete = row.get("complete")
        if complete is False or status in {"unknown", "not_checked"}:
            incomplete += 1
        elif status in {"up", "ok"}:
            passed += 1
        elif status in {"down", "degraded"}:
            faults += 1
        else:
            incomplete += 1
    # Coverage marks without a diagnosis row.
    for name, cov in (case.service_coverage or {}).items():
        if name in digs:
            continue
        if cov == "unresolved":
            incomplete += 1
        elif cov == "investigated":
            passed += 1

    investigated = passed + incomplete + faults
    if investigated <= 0:
        return (
            "SystemUp above is a baseline sync result, not fleet-wide "
            "dataplane verification. No services received additional "
            "dataplane investigation this run."
        )

    n = investigated
    svc = "service" if n == 1 else "services"
    only = f"Only {n} {svc} received additional dataplane investigation"
    preface = (
        "SystemUp above is a baseline sync result, not fleet-wide "
        "dataplane verification. "
    )
    if passed == n and incomplete == 0 and faults == 0:
        return (
            f"{preface}{only}; all passed PE-side readiness checks. "
            "Customer traffic delivery was not tested."
        )
    bits: list[str] = []
    if passed:
        bits.append(
            f"{passed} passed PE-side readiness checks"
        )
    if faults:
        bits.append(
            f"{faults} with dataplane "
            f"{'fault' if faults == 1 else 'faults'} (down/degraded)"
        )
    if incomplete:
        bits.append(
            f"{incomplete} inconclusive/incomplete"
        )
    detail = "; ".join(bits)
    return (
        f"{preface}{only}: {detail}. "
        "Customer traffic delivery was not tested."
    )

def _iter_service_records(services: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten flat collect map or nested ``{stype: {instances: [...]}}``."""
    out: list[dict[str, Any]] = []
    for key, group in (services or {}).items():
        if not isinstance(group, dict):
            continue
        if "instances" in group:
            stype = str(group.get("service_type") or key)
            for inst in group.get("instances") or []:
                if not isinstance(inst, dict):
                    continue
                rec = dict(inst)
                rec.setdefault("service_type", stype)
                out.append(rec)
            continue
        if group.get("name") or group.get("service_type"):
            rec = dict(group)
            if "/" in str(key) and not rec.get("service_type"):
                rec["service_type"] = str(key).split("/", 1)[0]
            out.append(rec)
    out.sort(
        key=lambda r: (
            str(r.get("service_type") or ""),
            str(r.get("name") or ""),
        )
    )
    return out


def _endpoint_lines(rec: dict[str, Any]) -> list[str]:
    """A/Z style lines from live_l2 endpoints or device list."""
    live = rec.get("live_l2") if isinstance(rec.get("live_l2"), dict) else {}
    eps = [e for e in (live.get("endpoints") or []) if isinstance(e, dict)]
    lines: list[str] = []
    if eps:
        labels = ("A", "Z")
        for i, ep in enumerate(eps):
            label = labels[i] if i < len(labels) else f"EP{i + 1}"
            device = ep.get("device") or "?"
            ac = ep.get("ac") or "?"
            bits = [f"{label}: {device} {ac}"]
            st = ep.get("st") or ep.get("ac_st")
            if st:
                bits.append(f"st={st}")
            err = ep.get("error")
            if err:
                bits.append(f"({err})")
            xc = ep.get("xconnect")
            if xc:
                bits.append(f"xc={xc}")
            lines.append("  " + " ".join(str(b) for b in bits))
        return lines
    devices = [d for d in (rec.get("devices") or []) if isinstance(d, str) and d]
    if devices:
        lines.append("  devices: " + ", ".join(devices))
    return lines


def format_service_details(services: dict[str, Any]) -> list[str]:
    """Per-instance detail for a scoped service list (type/id filter)."""
    records = _iter_service_records(services)
    if not records:
        return ["(none)"]
    lines: list[str] = []
    for rec in records:
        stype = rec.get("service_type") or "?"
        name = rec.get("name") or "?"
        status = rec.get("status") or "unknown"
        sys_s = rec.get("system_status") or "—"
        dp = rec.get("dataplane_status") or "—"
        lines.append(f"- {stype}/{name}: status={status} (system={sys_s}, dataplane={dp})")
        ep_lines = _endpoint_lines(rec)
        if ep_lines:
            lines.extend(ep_lines)
        else:
            lines.append("  (no endpoint details)")
    return lines


def render_report(
    case: CaseFile,
    *,
    summary: str | None = None,
    full: bool = False,
    services_detail: bool = False,
    duration_seconds: float | None = None,
    dry_run: bool = False,
    reporting_notes: list[str] | None = None,
    llm_model: str | None = None,
    case_delta: dict | None = None,
    previous_run_id: str | None = None,
    include_changes_section: bool = True,
) -> str:
    """Operator-facing diagnostic report (deterministic body; optional LLM summary)."""
    from diagnostic_mas.case_delta import format_changes_since_previous
    from diagnostic_mas.operator_report import (
        format_devices_operator,
        format_duration,
        format_followup_operator,
        format_result_line,
        format_run_details,
        format_services_operator,
        format_appendix_full,
        scope_counts,
        service_sync_note_from_case,
    )

    title = "# NSO Diagnostic Report"
    model = str(llm_model or "").strip()
    if model:
        title = f"# NSO Diagnostic Report (with {model})"
    lines: list[str] = [title, ""]

    n_dev, n_svc = scope_counts(case)
    duration = format_duration(duration_seconds)
    scope_bits = [f"{n_dev} device{'s' if n_dev != 1 else ''}"]
    scope_bits.append(f"{n_svc} service{'s' if n_svc != 1 else ''}")
    lines.append(f"**Scope:** {' · '.join(scope_bits)}")
    if duration:
        lines.append(f"**Duration:** {duration}")
    lines.append(f"**Result:** {format_result_line(case)}")
    lines.append("")

    # LLM (or caller-supplied) Summary / Result prose only at the top
    if summary and summary.strip():
        lines.append("## Summary")
        lines.append("")
        summary_body = re.sub(
            r"\A(?:#{1,6}[ \t]+Summary[ \t]*(?:#+[ \t]*)?(?:\r?\n|$)\s*)+",
            "",
            summary.strip(),
            flags=re.IGNORECASE,
        )
        lines.append(scrub_internal_ids(summary_body))
        lines.append("")

    if include_changes_section:
        # Prefer operational delta over repeating unchanged dig observations.
        lines.extend(
            format_changes_since_previous(
                case_delta,
                previous_run_id=previous_run_id,
            )
        )
        lines.append("")

    lines.append("## Devices")
    lines.append("")
    lines.extend(format_devices_operator(case, full=full))
    lines.append("")

    lines.append("## Services")
    lines.append("")
    counts = _service_counts_from_case(case)
    if counts:
        lines.extend(format_services_table(counts))
        lines.append("")
        sync_note = service_sync_note_from_case(case)
        if sync_note:
            lines.append(scrub_internal_ids(sync_note))
            lines.append("")
        lines.append(format_dataplane_dig_line(case))
        lines.append("")
    lines.extend(
        format_services_operator(case, services_detail=services_detail)
    )
    lines.append("")

    from diagnostic_mas.case_delta import format_last_known_faults
    if not include_changes_section or case_delta is None:
        lines.extend(format_last_known_faults(case.last_known_service_faults))
    lines.append("## Recommended follow-up")
    lines.append("")
    lines.extend(format_followup_operator(case))
    lines.append("")

    lines.append("## Run details")
    lines.append("")
    lines.extend(
        format_run_details(
            case, dry_run=dry_run, reporting_notes=reporting_notes
        )
    )
    lines.append("")

    if full:
        lines.extend(format_appendix_full(case))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
