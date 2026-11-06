"""Deterministic report from case Evidence / Issues / Hypotheses."""

from __future__ import annotations

import re

from typing import Any

from diagnostic_mas.case import CaseFile
from diagnostic_mas.case_delta import CaseDelta
from diagnostic_mas.roles.summary import scrub_internal_ids


def _dataplane_dig_subjects(case: CaseFile) -> dict[str, dict[str, Any]]:
    """Per-service dig outcome; prefer Diagnoses over Evidence."""
    rows: dict[str, dict[str, Any]] = {}
    for dx in case.diagnoses:
        if dx["kind"] != "dataplane":
            continue
        rows[dx["subject"]["name"]] = {"status": dx["status"], "complete": dx["complete"], "source": dx["source"]}
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


def format_dataplane_dig_line(case: CaseFile) -> str:
    """Coverage statement under the system-level services table."""
    from diagnostic_mas.port_investigation import SOURCES, device_findings, port_findings

    subjects = _dataplane_dig_subjects(case)
    explained = sum(1 for row in subjects.values() if row.get("source") in SOURCES)
    line = _dataplane_dig_line(
        case, {name: row for name, row in subjects.items() if row.get("source") not in SOURCES})
    if explained:
        counts = [(sum(1 for f in found(case) if f.get("concluded")), kind)
                  for found, kind in ((port_findings, "port"), (device_findings, "device"))]
        kept = sum(1 for f in port_findings(case) if f.get("earlier"))
        earlier = {"port": f" ({kept} from {'an earlier scan' if kept == 1 else 'earlier scans'})" if kept else ""}
        by = " and ".join(f"{n} {kind} investigation{'s' if n != 1 else ''}{earlier.get(kind, '')}"
                          for n, kind in counts if n)
        line += (
            f" {explained} service{'s are' if explained != 1 else ' is'} explained by {by}: "
            "Down on this scan's own evidence, not investigated individually.")
    return line


def _dataplane_dig_line(case: CaseFile, digs: dict[str, dict[str, Any]]) -> str:
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
            "Configuration sync and dataplane are independent; sync is not "
            "dataplane verification. No services received additional "
            "dataplane investigation this run."
        )

    n = investigated
    svc = "service" if n == 1 else "services"
    only = f"Only {n} {svc} received additional dataplane investigation"
    preface = (
        "Configuration sync and dataplane are independent; sync is not "
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


_SUMMARY_SUB_BULLET_LABEL = re.compile(r"^([ \t]+[-*][ \t]+)(Cause|Next):", re.MULTILINE)


def _bold_summary_labels(text: str) -> str:
    """Bold the Cause/Next label that opens a Summary sub-bullet."""
    return _SUMMARY_SUB_BULLET_LABEL.sub(r"\1**\2:**", text)


def render_report(
    case: CaseFile,
    *,
    summary: str | None = None,
    services_detail: bool = False,
    duration_seconds: float | None = None,
    dry_run: bool = False,
    reporting_notes: list[str] | None = None,
    llm_model: str | None = None,
    case_delta: CaseDelta | None = None,
    previous_run_id: str | None = None,
    include_changes_section: bool = True,
) -> str:
    """Operator-facing diagnostic report (deterministic body; optional LLM summary)."""
    from diagnostic_mas.case_delta import format_changes_since_previous
    from diagnostic_mas.operator_report import (
        format_devices_operator,
        format_duration,
        format_followup_operator,
        format_result_section,
        format_run_details,
        format_services_operator,
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
    from diagnostic_mas.newness import newness_for

    # What is new since the previous scan, for the Result's tags and the follow-up labels.
    newness = newness_for(case, case_delta)
    lines.extend(format_result_section(case, newness))
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
        lines.append(_bold_summary_labels(scrub_internal_ids(summary_body)))
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
    lines.extend(format_devices_operator(case))
    lines.append("")

    from diagnostic_mas.service_presence import disappearance_section
    lines.extend(disappearance_section(case))
    lines.append("## Services")
    lines.append("")
    from diagnostic_mas.service_final_status import final_service_counts, format_final_services_table
    counts = final_service_counts(case)
    if counts:
        lines.extend(format_final_services_table(counts))
        lines.append("")
        lines.append("Each service is counted once; the four final-status columns sum to Total.")
        lines.append("")
        sync_note = service_sync_note_from_case(case)
        if sync_note:
            lines.append(scrub_internal_ids(sync_note))
            lines.append("")
        from diagnostic_mas.dataplane_verify import iter_service_records
        basic_counts = {}
        for _, record in iter_service_records(case):
            from diagnostic_mas.service_presence import disappeared
            if disappeared(record):
                continue
            basic = record.get("basic_checks")
            if isinstance(basic, dict):
                status = basic.get("status", "unknown")
                bucket = basic_counts.setdefault(record.get("service_type", "service"), {})
                bucket[status] = bucket.get(status, 0) + 1
        for service_type, bucket in sorted(basic_counts.items()):
            lines.append(f"**{service_type} basic operational checks:** " + " · ".join(
                f"{label}: {bucket.get(status, 0)}" for status, label in
                [("up", "Up"), ("down", "Down"), ("unknown", "Unknown"), ("not_checked", "Not checked")]
            ) + ". Separate from LLM dataplane verification; customer delivery not tested.")
            lines.append("")
        lines.append(format_dataplane_dig_line(case))
        lines.append("")
    for evidence in case.evidence:
        if evidence.get("kind") == "spine" and evidence.get("role") == "service":
            retries = (evidence.get("payload", {}).get("extra") or {}).get("sync_rechecks") or {}
            if retries:
                lines.extend(["**Targeted device-sync rechecks:**", ""])
                for device, result in sorted(retries.items()):
                    initial = result.get("fleet_result") or "missing"
                    effective = result.get("effective_result") or "unknown"
                    outcome = effective if result.get("resolved") else (
                        f"skipped ({result['skipped']})" if result.get("skipped") else "inconclusive"
                    ) + "; Sync Unknown retained"
                    lines.append(f"- {device}: fleet `{initial}` → retry {outcome}.")
                lines.append("")
    from diagnostic_mas.unknown_breakdown import format_unknown_breakdown
    lines.extend(format_unknown_breakdown(case))
    lines.extend(
        format_services_operator(case, services_detail=services_detail)
    )
    lines.append("")

    from diagnostic_mas.case_delta import format_last_known_faults
    if not include_changes_section or case_delta is None:
        lines.extend(format_last_known_faults(case.last_known_service_faults))
    lines.append("## Recommended follow-up")
    lines.append("")
    lines.extend(format_followup_operator(case, newness))
    lines.append("")

    from diagnostic_mas.run_configuration import format_run_configuration
    lines.extend(format_run_configuration(case))
    lines.append("## Run details")
    lines.append("")
    lines.extend(
        format_run_details(
            case, dry_run=dry_run, reporting_notes=reporting_notes
        )
    )
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"
