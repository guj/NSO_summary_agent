"""Summary role: narrative only — never writes Evidence."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from agent.config import Settings
from diagnostic_mas.case import CaseFile

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

# Internal case ids must not appear in operator-facing narrative
_INTERNAL_ID_RE = re.compile(
    r"\b(?:is|ev|ho|hy)_\d+\b",
    re.IGNORECASE,
)
_GROUNDED_IN_IDS_RE = re.compile(
    r"\(?\s*Grounded in\s+[^)]*(?:is|ev|ho|hy)_\d+[^)]*\)?",
    re.IGNORECASE,
)


def scrub_internal_ids(text: str) -> str:
    """Remove is_/ev_/ho_/hy_ citations from model prose."""
    cleaned = _GROUNDED_IN_IDS_RE.sub("", text)
    cleaned = _INTERNAL_ID_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" +\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def case_has_drill_evidence(case: CaseFile) -> bool:
    return any(e.get("kind") in {"drill", "drill_finding"} for e in case.evidence)


def case_has_dataplane_evidence(case: CaseFile) -> bool:
    if any(d.get("kind") == "dataplane" for d in case.diagnoses):
        return True
    return any(
        e.get("kind") in {"dataplane_finding", "dataplane_incomplete"}
        for e in case.evidence
    )


def _dataplane_conclusion_rows(case: CaseFile) -> list[dict[str, Any]]:
    """Operator-facing dataplane rows; prefer Diagnoses over Evidence."""
    rows: list[dict[str, Any]] = []
    for dx in case.diagnoses:
        if dx.get("kind") != "dataplane":
            continue
        subject = dx.get("subject") if isinstance(dx.get("subject"), dict) else {}
        rows.append(
            {
                "name": subject.get("name") or dx.get("name"),
                "service_type": subject.get("service_type") or dx.get("service_type"),
                "dataplane_status": dx.get("status") or dx.get("dataplane_status"),
                "observed": dx.get("observed"),
                "cause": dx.get("cause"),
                "fix_suggestion": dx.get("fix_suggestion"),
                "source": dx.get("source") or "llm",
                "confidence": dx.get("confidence"),
                "complete": dx.get("complete", True),
            }
        )
    if rows:
        return rows
    for ev in case.evidence:
        if ev.get("kind") not in {"dataplane_finding", "dataplane_incomplete"}:
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if not payload:
            continue
        rows.append(
            {
                "name": payload.get("name") or payload.get("service_name"),
                "service_type": payload.get("service_type"),
                "dataplane_status": payload.get("dataplane_status"),
                "observed": payload.get("observed"),
                "cause": payload.get("cause"),
                "fix_suggestion": payload.get("fix_suggestion"),
                "source": payload.get("source") or "llm",
                "confidence": payload.get("confidence"),
                "complete": payload.get(
                    "complete", ev.get("kind") != "dataplane_incomplete"
                ),
            }
        )
    return rows


def dataplane_tally_for_summary(case: CaseFile) -> dict[str, Any]:
    """Authoritative dig counts for the summary LLM (avoids undercount drift).

    Incomplete / unresolved = ``complete is False`` or status in
    ``unknown`` / ``not_checked``. Names are listed per service_type so the
    model can group without inventing a lower instance count.
    """
    rows = _dataplane_conclusion_rows(case)
    by_status: dict[str, int] = {}
    by_type: dict[str, dict[str, Any]] = {}
    for p in rows:
        st = str(p.get("dataplane_status") or "unknown").strip().lower() or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
        incomplete = p.get("complete") is False or st in {
            "unknown",
            "not_checked",
        }
        if not incomplete:
            continue
        stype = (
            str(p.get("service_type") or "unknown").strip().lower() or "unknown"
        )
        name = str(p.get("name") or "?").strip() or "?"
        bucket = by_type.setdefault(stype, {"count": 0, "names": []})
        bucket["count"] = int(bucket["count"]) + 1
        names = bucket["names"]
        assert isinstance(names, list)
        names.append(name)
    return {
        "digs_total": len(rows),
        "by_status": by_status,
        "incomplete_or_unresolved_total": sum(
            int(b["count"]) for b in by_type.values()
        ),
        "incomplete_or_unresolved_by_type": by_type,
    }


_SUMMARY_SNIPPET_MAX = 240


def _snip(text: str | None, *, limit: int = _SUMMARY_SNIPPET_MAX) -> str:
    raw = " ".join(str(text or "").split())
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 1)].rstrip() + "…"


def narrative_from_dataplane_findings(case: CaseFile) -> str:
    """Short operator Summary skim from diagnoses (or legacy dataplane evidence).

    Full Observed detail belongs under Services — keep Summary scannable.
    """
    from diagnostic_mas.operator_report import (
        NEXT_SCOPED_REACHABILITY,
        RESULT_PE_SIDE_PASSED,
        UNCERTAINTY_DELIVERY_UNVERIFIED,
        _incomplete_dig_next_action,
        _incomplete_dig_stop_kind,
    )

    findings = _dataplane_conclusion_rows(case)
    if not findings:
        return ""
    blocks: list[str] = []
    for p in findings:
        name = p.get("name") or "?"
        stype = p.get("service_type") or ""
        dp = p.get("dataplane_status") or "?"
        title = f"{stype} {name}".strip() if stype else str(name)
        complete = p.get("complete", True)
        if complete is False:
            head = f"- **{title}** — dataplane={dp} (incomplete; unresolved)"
        else:
            head = f"- **{title}** — dataplane={dp}"
        lines = [head]
        dp_l = str(dp or "").lower()
        if complete is not False and dp_l in {"up", "ok"}:
            lines.append(
                f"  - Cause: {RESULT_PE_SIDE_PASSED} "
                f"{UNCERTAINTY_DELIVERY_UNVERIFIED}"
            )
            fix = _snip(p.get("fix_suggestion"))
            lines.append(f"  - Next: {fix or NEXT_SCOPED_REACHABILITY}")
        else:
            cause = _snip(p.get("cause"))
            if cause:
                lines.append(f"  - Cause: {cause}")
            if complete is False:
                kind = _incomplete_dig_stop_kind(p)
                if kind == "timeout":
                    lines.append(
                        "  - Next: Address the dataplane LLM timeout, then "
                        "re-run the dig (not a tool-budget issue)."
                    )
                else:
                    # Prefer deterministic timeout/budget wording over generic.
                    next_act = _incomplete_dig_next_action(p)
                    lines.append(f"  - Next: {next_act}")
            else:
                fix = _snip(p.get("fix_suggestion"))
                if fix:
                    lines.append(f"  - Next: {fix}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def _template_narrative(case: CaseFile, *, skip_llm: bool) -> str:
    open_n = sum(1 for i in case.issues if i.get("status") == "open")
    needs = sum(1 for i in case.issues if i.get("status") == "needs_human")
    explained = sum(1 for i in case.issues if i.get("status") == "explained")
    lines = [
        f"Evidence records: {len(case.evidence)}; "
        f"issues open={open_n} explained={explained} needs_human={needs}; "
        f"handoffs={len(case.handoffs)}."
    ]
    if case.hypotheses:
        lines.append(
            f"Hypotheses present: {len(case.hypotheses)} "
            "(labeled only; not ground truth)."
        )
    if skip_llm:
        lines.append(
            "LLM diagnosis skipped; report is MCP spine Evidence/Issues only."
        )
    return " ".join(lines)


def _compact_case_for_llm(
    case: CaseFile,
    *,
    case_delta: dict[str, Any] | None = None,
    previous_run_id: str | None = None,
) -> dict[str, Any]:
    """Operator-facing fields only — omit internal is_/ev_/ho_/hy_ ids."""
    evidence_brief = []
    for ev in case.evidence[:40]:
        row: dict[str, Any] = {
            "kind": ev.get("kind"),
            "role": ev.get("role"),
            "layer": ev.get("layer"),
        }
        if ev.get("kind") == "drill":
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            if payload.get("check"):
                row["check"] = payload.get("check")
            if payload.get("args") is not None:
                row["args"] = payload.get("args")
            if payload.get("reason"):
                row["reason"] = payload.get("reason")
            if payload.get("error") is not None:
                row["error"] = payload.get("error")
            result = payload.get("result")
            if result is not None:
                text = json.dumps(result, default=str)
                row["result"] = text if len(text) <= 800 else text[:800] + "…"
        elif ev.get("kind") == "drill_finding":
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            for key in (
                "observed",
                "cause",
                "fix_suggestion",
                "fix_requires_human_approval",
                "confidence",
                "issue_edge_id",
            ):
                if key in payload and payload.get(key) is not None:
                    row[key] = payload.get(key)
        elif ev.get("kind") == "dataplane_finding":
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            for key in (
                "name",
                "service_name",
                "service_type",
                "dataplane_status",
                "observed",
                "cause",
                "fix_suggestion",
                "confidence",
                "source",
            ):
                if key in payload and payload.get(key) is not None:
                    row[key] = payload.get(key)
        elif ev.get("kind") == "dataplane_incomplete":
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            row["complete"] = False
            for key in (
                "name",
                "service_name",
                "service_type",
                "dataplane_status",
                "observed",
                "cause",
                "source",
            ):
                if key in payload and payload.get(key) is not None:
                    row[key] = payload.get(key)
        evidence_brief.append(row)
    issues_brief = []
    for i in case.issues[:40]:
        brief: dict[str, Any] = {
            "status": i.get("status"),
            "code": i.get("code"),
            "message": i.get("message"),
            "layer": i.get("layer"),
            "edge_id": i.get("edge_id"),
        }
        if i.get("devices"):
            brief["devices"] = list(i["devices"])
        if isinstance(i.get("live_l2"), dict):
            brief["live_l2"] = i["live_l2"]
        if i.get("system_status"):
            brief["system_status"] = i.get("system_status")
        if i.get("dataplane_status"):
            brief["dataplane_status"] = i.get("dataplane_status")
        if "in_sync" in i:
            brief["in_sync"] = i.get("in_sync")
        if isinstance(i.get("device_sync"), dict):
            brief["device_sync"] = i["device_sync"]
        issues_brief.append(brief)
    # Keep every dataplane diagnosis (fleet digs often exceed 20). Cap other
    # kinds so the compact payload stays bounded.
    diagnoses_brief = []
    other_dx = 0
    for dx in case.diagnoses:
        kind = str(dx.get("kind") or "")
        if kind != "dataplane":
            if other_dx >= 20:
                continue
            other_dx += 1
        row: dict[str, Any] = {
            "kind": dx.get("kind"),
            "source": dx.get("source"),
            "status": dx.get("status"),
            "complete": dx.get("complete", True),
        }
        subject = dx.get("subject") if isinstance(dx.get("subject"), dict) else {}
        if subject.get("name"):
            row["name"] = subject.get("name")
        if subject.get("service_type"):
            row["service_type"] = subject.get("service_type")
        for key in ("observed", "cause", "fix_suggestion", "confidence"):
            if key in dx and dx.get(key) is not None:
                # Snip long Observed for summary context; Cause stays fuller.
                val = dx.get(key)
                if key == "observed" and isinstance(val, str) and len(val) > 400:
                    row[key] = val[:399].rstrip() + "…"
                else:
                    row[key] = val
        if isinstance(dx.get("verification_gap"), dict):
            row["verification_gap"] = dx["verification_gap"]
        if isinstance(dx.get("investigation"), dict):
            row["investigation"] = dx["investigation"]
        diagnoses_brief.append(row)
    tally = dataplane_tally_for_summary(case)
    digs_n = int(tally.get("digs_total") or 0)
    dp_used = int(getattr(case.budget, "dataplane_tools_used", 0) or 0)
    from diagnostic_mas.dataplane_verify import dataplane_tools_cap as _dp_cap

    per_dig_cap = _dp_cap(case.budget)
    out: dict[str, Any] = {
        "evidence": evidence_brief,
        "issues": issues_brief,
        "diagnoses": diagnoses_brief,
        "dataplane_tally": tally,
        "dataplane_budget": {
            "tools_used_total": dp_used,
            "max_tools_per_dig": per_dig_cap,
            "digs": digs_n,
            "note": (
                "max_tools_per_dig (--max-dataplane-tools) is a per-dig "
                "ceiling, not a fleet total. tools_used_total may exceed "
                "max_tools_per_dig across many digs; that is normal and "
                "not a tooling constraint. Do not recommend raising "
                "--max-dataplane-tools from aggregate totals alone — only "
                "when a Diagnosis shows a dig hit the per-dig tool cap "
                "before concluding."
            ),
        },
        "handoffs": [
            {
                "from_role": h.get("from_role"),
                "to_role": h.get("to_role"),
                "reason": h.get("reason"),
            }
            for h in case.handoffs[:20]
        ],
        "hypotheses": [
            {"text": scrub_internal_ids(str(h.get("text") or ""))}
            for h in case.hypotheses[:20]
        ],
        "issues_total": len(case.issues),
        "drill_issues_used": case.budget.drill_issues_used,
        "max_drill_issues": case.budget.max_drill_issues,
        "drills_used": case.budget.drills_used,
        # Legacy aliases (same semantics as dataplane_budget.*).
        "dataplane_tools_used": dp_used,
        "max_dataplane_tools": per_dig_cap,
        "max_tools_per_drill": case.budget.max_tools_per_drill,
    }
    if case_delta is not None:
        out["changes_since_previous"] = case_delta
        if previous_run_id:
            out["previous_run_id"] = previous_run_id
    return out


def _system_prompt_for_summary(*, pinned: bool) -> str:
    name = "summary_pinned.txt" if pinned else "summary.txt"
    path = _PROMPTS / name
    if path.is_file():
        return path.read_text(encoding="utf-8")
    if pinned:
        return (
            "Write a SHORT executive skim for ## Summary. "
            "Per service: status, short Cause, short Next. "
            "Do not paste full Observed. Ground claims in Evidence only."
        )
    return (
        "Write a SHORT executive skim for ## Summary. "
        "Do not invent topology facts or paste full Observed."
    )


def _llm_summary(
    settings: Settings,
    case: CaseFile,
    *,
    pinned: bool = False,
    case_delta: dict[str, Any] | None = None,
    previous_run_id: str | None = None,
) -> str:
    from agent.summarize import FABRIC_CHAT_TIMEOUT_SEC, fabric_openai_client

    system = _system_prompt_for_summary(pinned=pinned)
    compact = _compact_case_for_llm(
        case, case_delta=case_delta, previous_run_id=previous_run_id
    )
    user = json.dumps(compact, default=str)
    sys_n = len(system)
    user_n = len(user)
    print(
        f"[summary] LLM request model={settings.fabric_model!r} "
        f"pinned={pinned} timeout={FABRIC_CHAT_TIMEOUT_SEC:g}s "
        f"system={sys_n} chars user={user_n} chars "
        f"total≈{sys_n + user_n} chars "
        f"(evidence={len(compact.get('evidence') or [])} "
        f"issues={compact.get('issues_total')} "
        f"diagnoses={len(compact.get('diagnoses') or [])})",
        file=sys.stderr,
    )
    dump = (os.environ.get("SUMMARY_LLM_DUMP") or "").strip()
    if dump:
        path = Path(dump)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "model": settings.fabric_model,
                    "pinned": pinned,
                    "timeout_sec": FABRIC_CHAT_TIMEOUT_SEC,
                    "system": system,
                    "user": compact,
                    "user_json_chars": user_n,
                    "system_chars": sys_n,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"[summary] dumped request to {path}", file=sys.stderr)

    # Keep summary bounded — default OpenAI SDK 600s hangs dominate lean runs.
    client = fabric_openai_client(settings, timeout=FABRIC_CHAT_TIMEOUT_SEC)
    t0 = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=settings.fabric_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
    except Exception:
        print(
            f"[summary] LLM failed after {time.monotonic() - t0:.2f}s "
            f"(system={sys_n} user={user_n} total≈{sys_n + user_n})",
            file=sys.stderr,
        )
        raise
    print(
        f"[summary] LLM completed in {time.monotonic() - t0:.2f}s "
        f"(system={sys_n} user={user_n} total≈{sys_n + user_n})",
        file=sys.stderr,
    )
    return ((resp.choices[0].message.content or "") if resp.choices else "").strip()


def _format_summary_llm_failure(settings: Settings, exc: BaseException) -> str:
    from agent.summarize import FABRIC_CHAT_TIMEOUT_SEC

    return (
        f"LLM summary failed: {exc} "
        f"(final summary chat; model={settings.fabric_model!r}; "
        f"timeout={FABRIC_CHAT_TIMEOUT_SEC:g}s)"
    )


async def summary_narrative(
    case: CaseFile,
    settings: Settings,
    *,
    skip_llm: bool = True,
    llm_summary_fn: Any | None = None,
    pinned: bool | None = None,
    deterministic_summary: bool = False,
    case_delta: dict[str, Any] | None = None,
    previous_run_id: str | None = None,
) -> str:
    """Build the operator Summary section.

    Default (when LLM is enabled): one FABRIC chat over the compact case.
    With ``deterministic_summary=True``, restate dataplane diagnoses/findings
    with no final chat (fallback to the template if none are ready).
    """
    deterministic = narrative_from_dataplane_findings(case)

    if deterministic_summary:
        if deterministic:
            print(
                "[summary] deterministic dataplane findings/diagnoses "
                "(no final LLM chat)",
                file=sys.stderr,
            )
            return scrub_internal_ids(deterministic)
        print(
            "[summary] deterministic requested but no dataplane findings; "
            "using template",
            file=sys.stderr,
        )
        return _template_narrative(case, skip_llm=skip_llm)

    if skip_llm or not getattr(settings, "fabric_api_key", None):
        if deterministic:
            return scrub_internal_ids(deterministic)
        return _template_narrative(case, skip_llm=True)

    use_pinned = (
        case_has_drill_evidence(case) or case_has_dataplane_evidence(case)
        if pinned is None
        else pinned
    )
    try:
        if llm_summary_fn is not None:
            text = llm_summary_fn(settings, case)
        else:
            text = _llm_summary(
                settings,
                case,
                pinned=use_pinned,
                case_delta=case_delta,
                previous_run_id=previous_run_id,
            )
        if text:
            return scrub_internal_ids(text)
    except Exception as exc:  # noqa: BLE001
        note = _format_summary_llm_failure(settings, exc)
        print(note, file=sys.stderr)
        if deterministic:
            return scrub_internal_ids(deterministic) + " " + note
        return _template_narrative(case, skip_llm=False) + " " + note
    return _template_narrative(case, skip_llm=False)
