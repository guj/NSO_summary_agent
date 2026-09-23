"""Case file: Evidence, Issues, budget, and stop predicates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

IssueStatus = Literal[
    "open", "explained", "escalated", "needs_human", "budget_exhausted"
]


@dataclass
class Budget:
    max_deep_checks: int
    max_handoffs: int
    deep_checks_used: int = 0
    handoffs_used: int = 0
    # How many Issues get a drill session (e.g. 2 degraded services)
    max_drill_issues: int = 2
    drill_issues_used: int = 0
    # Tool-call allowance per drill session (generous)
    max_tools_per_drill: int = 12
    # MCP tool calls per service in the dataplane verify LLM phase
    max_dataplane_tools: int = 40
    # Total MCP tool calls across all dataplane dig sessions (reporting)
    dataplane_tools_used: int = 0
    # Total MCP tool calls across all drill sessions (reporting; not dataplane)
    drills_used: int = 0


@dataclass
class DrillSession:
    """Per-issue tool budget for one drill investigation."""

    max_tools: int
    tools_used: int = 0
    issue_id: str | None = None
    issue_edge_id: str | None = None


@dataclass
class CaseFile:
    budget: Budget
    evidence: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    # Attributed conclusions (LLM/fallback). Not Evidence — cite evidence_ids.
    diagnoses: list[dict[str, Any]] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    plans: list[dict[str, Any]] = field(default_factory=list)
    handoffs: list[dict[str, Any]] = field(default_factory=list)
    device_names: list[str] = field(default_factory=list)
    # Positive current-run live-query evidence; inventory alone is insufficient.
    live_verified_devices: list[str] = field(default_factory=list)
    # Report focus (e.g. --devices renc). ISIS/BGP still collect the full pocket
    # for bidirectional pairing; report sections filter to these names when set.
    focus_devices: list[str] = field(default_factory=list)
    # Per service name: basic_passed | investigated | unresolved | budget_skipped
    # | needs_investigation | category_peer_skipped | llm_budget_exceeded
    service_coverage: dict[str, str] = field(default_factory=dict)
    # Set when provider LLM spend budget is exceeded; stop further LLM calls.
    # Historical faults are not current evidence or fresh diagnoses.
    last_known_service_faults: list[dict[str, Any]] = field(default_factory=list)
    llm_halt_reason: str | None = None
    _evidence_seq: int = 0
    _issue_seq: int = 0
    _diagnosis_seq: int = 0


def add_evidence(case: CaseFile, record: dict[str, Any]) -> str:
    """Append Evidence; assign id if missing. Only collectors should call this."""
    rec = dict(record)
    if not rec.get("id"):
        case._evidence_seq += 1
        rec["id"] = f"ev_{case._evidence_seq}"
    case.evidence.append(rec)
    return str(rec["id"])


def evidence_ids_after(case: CaseFile, seq_before: int) -> list[str]:
    """Evidence ids assigned after ``seq_before`` (value of ``_evidence_seq``)."""
    out: list[str] = []
    for ev in case.evidence:
        eid = ev.get("id")
        if not isinstance(eid, str) or not eid.startswith("ev_"):
            continue
        try:
            n = int(eid.split("_", 1)[1])
        except ValueError:
            continue
        if n > seq_before:
            out.append(eid)
    return out


def add_diagnosis(
    case: CaseFile,
    *,
    kind: str,
    source: str,
    observed: str,
    cause: str,
    status: str | None = None,
    subject: dict[str, Any] | None = None,
    fix_suggestion: str | None = None,
    confidence: str | None = None,
    evidence_ids: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Append an attributed diagnosis (not Evidence). Citations via evidence_ids."""
    case._diagnosis_seq += 1
    diag_id = f"dx_{case._diagnosis_seq}"
    rec: dict[str, Any] = {
        "id": diag_id,
        "kind": kind,
        "source": source,
        "observed": observed,
        "cause": cause,
        "evidence_ids": list(evidence_ids or []),
    }
    if status is not None:
        rec["status"] = status
    if subject:
        rec["subject"] = dict(subject)
    if fix_suggestion is not None:
        rec["fix_suggestion"] = fix_suggestion
    if confidence is not None:
        rec["confidence"] = confidence
    if extra:
        for key, value in extra.items():
            if key not in rec and value is not None:
                rec[key] = value
    case.diagnoses.append(rec)
    return diag_id


_UNSET: Any = object()


def open_issue(
    case: CaseFile,
    *,
    code: str,
    message: str,
    evidence_ids: list[str],
    severity: str = "medium",
    layer: str | None = None,
    edge_id: str | None = None,
    devices: list[str] | None = None,
    live_l2: dict[str, Any] | None = None,
    device_sync: dict[str, Any] | None = None,
    in_sync: Any = _UNSET,
    system_status: str | None = None,
    dataplane_status: str | None = None,
) -> str:
    case._issue_seq += 1
    issue_id = f"is_{case._issue_seq}"
    rec: dict[str, Any] = {
        "id": issue_id,
        "code": code,
        "message": message,
        "evidence_ids": list(evidence_ids),
        "severity": severity,
        "layer": layer,
        "edge_id": edge_id,
        "status": "open",
    }
    if devices:
        rec["devices"] = list(devices)
    if isinstance(live_l2, dict):
        rec["live_l2"] = live_l2
    if device_sync is not None:
        rec["device_sync"] = dict(device_sync)
    if in_sync is not _UNSET:
        rec["in_sync"] = in_sync
    if system_status:
        rec["system_status"] = system_status
    if dataplane_status:
        rec["dataplane_status"] = dataplane_status
    case.issues.append(rec)
    return issue_id


def set_issue_status(case: CaseFile, issue_id: str, status: IssueStatus) -> None:
    for issue in case.issues:
        if issue.get("id") == issue_id:
            issue["status"] = status
            return
    raise KeyError(f"unknown issue_id: {issue_id}")


def debit_deep_check(case: CaseFile, n: int = 1) -> bool:
    b = case.budget
    if b.deep_checks_used + n > b.max_deep_checks:
        return False
    b.deep_checks_used += n
    return True


def debit_handoff(case: CaseFile, n: int = 1) -> bool:
    b = case.budget
    if b.handoffs_used + n > b.max_handoffs:
        return False
    b.handoffs_used += n
    return True


def debit_drill(
    case: CaseFile,
    n: int = 1,
    *,
    session: DrillSession | None = None,
    account: Literal["drill", "dataplane"] = "drill",
) -> bool:
    """Debit drill or dataplane tool call(s). Prefer a per-issue ``session`` cap."""
    b = case.budget
    if session is not None:
        if session.tools_used + n > session.max_tools:
            return False
        session.tools_used += n
        if account == "dataplane":
            b.dataplane_tools_used += n
        else:
            b.drills_used += n
        return True
    # No session: global cap = issues × tools-per-issue (drill pool only)
    if account == "dataplane":
        return False
    cap = max(0, b.max_drill_issues) * max(0, b.max_tools_per_drill)
    if b.drills_used + n > cap:
        return False
    b.drills_used += n
    return True


def begin_drill_issue(case: CaseFile) -> bool:
    """Reserve one drill-issue slot. False if issue budget exhausted."""
    b = case.budget
    if b.drill_issues_used + 1 > b.max_drill_issues:
        return False
    b.drill_issues_used += 1
    return True


def should_stop(case: CaseFile) -> bool:
    if not any(i.get("status") == "open" for i in case.issues):
        return True
    b = case.budget
    if (
        b.deep_checks_used >= b.max_deep_checks
        and b.handoffs_used >= b.max_handoffs
    ):
        return True
    return False
