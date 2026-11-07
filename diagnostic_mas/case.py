"""Case file: Evidence, Issues, budget, and stop predicates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict

IssueStatus = Literal[
    "open", "explained", "escalated", "needs_human", "budget_exhausted"
]


class Issue(TypedDict):
    """A problem the scan found. ``open_issue`` sets every field below; the rest only when known."""
    id: str
    code: str
    message: str
    evidence_ids: list[str]
    severity: str
    layer: str | None
    edge_id: str | None
    status: str
    devices: NotRequired[list[str]]
    live_l2: NotRequired[dict[str, Any]]
    device_sync: NotRequired[dict[str, Any]]
    in_sync: NotRequired[Any]
    system_status: NotRequired[str]
    dataplane_status: NotRequired[str]


# The fields every issue has, with what an issue from outside a run gets when it lacks one.
_ISSUE_DEFAULTS: dict[str, Any] = {
    "id": "", "code": "unknown", "message": "", "evidence_ids": [], "severity": "medium",
    "layer": None, "edge_id": None, "status": "open",
}
ISSUE_FIELDS = tuple(_ISSUE_DEFAULTS)


def normalize_issue(raw: Any) -> Issue | None:
    """An issue from outside this run, such as a saved scan's, given the issue shape; None if it is not one.

    Readers then take the fields as they are, so a missing one fails loudly instead of reading as empty.
    """
    if not isinstance(raw, dict):
        return None
    issue = dict(raw)
    for key, default in _ISSUE_DEFAULTS.items():
        if issue.get(key) is None and (key not in issue or default is not None):
            issue[key] = list(default) if isinstance(default, list) else default
    return issue  # type: ignore[return-value]


class DiagnosisSubject(TypedDict):
    service_type: str | None
    name: str


class Diagnosis(TypedDict):
    """A service's dataplane conclusion. ``add_diagnosis`` sets every field below; the last three only when known."""
    id: str
    kind: str  # "dataplane"
    source: str  # llm, gate, fallback, port_investigation or device_investigation
    observed: str
    cause: str
    evidence_ids: list[str]
    status: str  # up, down, degraded or unknown
    subject: DiagnosisSubject
    confidence: str  # high, medium or low
    complete: bool  # False: the investigation did not finish, so the service's issue stays open
    fix_suggestion: NotRequired[str]
    verification_gap: NotRequired[dict[str, Any]]
    investigation: NotRequired[dict[str, Any]]


# The fields every diagnosis has besides ``subject``, with what one from outside a run gets when it lacks one.
_DIAGNOSIS_DEFAULTS: dict[str, Any] = {
    "id": "", "kind": "", "source": "", "observed": "", "cause": "", "evidence_ids": [],
    "status": "unknown", "confidence": "medium", "complete": True,
}
DIAGNOSIS_FIELDS = (*_DIAGNOSIS_DEFAULTS, "subject")
# Where older scans kept a diagnosis's service and status.
_LEGACY_DIAGNOSIS_KEYS = ("name", "service_name", "service_type", "dataplane_status")


def normalize_diagnosis(raw: Any) -> Diagnosis | None:
    """A diagnosis from outside this run, such as a saved scan's, given the diagnosis shape.

    None if it is not one: not a dict, or naming no service, since every reader finds a diagnosis by its
    service. A service or status kept where older scans kept them moves to where it is now.
    """
    if not isinstance(raw, dict):
        return None
    diagnosis = {key: value for key, value in raw.items() if key not in _LEGACY_DIAGNOSIS_KEYS}
    subject = raw.get("subject") if isinstance(raw.get("subject"), dict) else {}
    name = str(subject.get("name") or raw.get("name") or raw.get("service_name") or "").strip()
    if not name:
        return None
    diagnosis["subject"] = {"service_type": subject.get("service_type") or raw.get("service_type"), "name": name}
    diagnosis["status"] = str(raw.get("status") or raw.get("dataplane_status") or "").lower() or None
    for key, default in _DIAGNOSIS_DEFAULTS.items():
        if diagnosis.get(key) is None:
            diagnosis[key] = list(default) if isinstance(default, list) else default
    for key in ("fix_suggestion", "verification_gap", "investigation"):
        if diagnosis.get(key) is None:
            diagnosis.pop(key, None)
    return diagnosis  # type: ignore[return-value]


class BasicChecks(TypedDict):
    """A service's fixed-check result: always a status and the checks. Other fields vary by service type."""
    status: str  # up, down, degraded, unknown or not_checked
    checks: list[dict[str, Any]]  # each has check, status and observation; device when known
    intent: NotRequired[dict[str, Any] | None]  # what NSO's config asks for; missing or None on some


class ServiceRecord(TypedDict):
    """A listed service, keyed ``<service_type>/<name>``; only the fields shaped so far are named here.

    Records also hold devices, device_sync, the status fields and more, not yet given defaults. A record
    without ``basic_checks`` was not checked (older scans), so it gets none.
    """
    name: str
    service_type: str
    basic_checks: NotRequired[BasicChecks]
    presence_recheck: NotRequired[dict[str, Any]]  # set when the service was rechecked as possibly removed
    live_l2: NotRequired[dict[str, Any]]  # live L2 readings, on l2ptp services only


def normalize_service_record(key: str, raw: Any) -> ServiceRecord | None:
    """A saved or hand-made service record given the record shape; None if it is not one.

    Its name and type come from its ``<service_type>/<name>`` key when it lacks them. The record is
    returned unchanged, not copied, when it already has the shape.
    """
    if not isinstance(raw, dict):
        return None
    kind, name = str(key).split("/", 1)[0], str(key).split("/", 1)[-1]
    fill: dict[str, Any] = {}
    if not raw.get("name"):
        fill["name"] = name
    if not raw.get("service_type"):
        fill["service_type"] = kind
    basic = raw.get("basic_checks")
    drop = "basic_checks" in raw and not isinstance(basic, dict)
    if isinstance(basic, dict) and (basic.get("status") is None or not isinstance(basic.get("checks"), list)):
        fill["basic_checks"] = {**basic, "status": basic.get("status") or "not_checked",
                                "checks": basic.get("checks") if isinstance(basic.get("checks"), list) else []}
    if not fill and not drop:
        return raw  # type: ignore[return-value]
    record = {key: value for key, value in raw.items() if not (drop and key == "basic_checks")}
    return {**record, **fill}  # type: ignore[return-value]


@dataclass
class Budget:
    max_deep_checks: int
    max_handoffs: int
    deep_checks_used: int = 0
    handoffs_used: int = 0
    # How many Issues get a drill session (e.g. 2 degraded services)
    max_drill_issues: int = 2
    drill_issues_used: int = 0
    # Drill sessions whose LLM loop ended without concluding (e.g. the provider refused the request)
    drill_issues_unconcluded: int = 0
    # Tool-call allowance per drill session (generous)
    max_tools_per_drill: int = 12
    # MCP tool calls per service in the dataplane verify LLM phase
    max_dataplane_tools: int = 40
    # Total MCP tool calls across all dataplane dig sessions (reporting)
    dataplane_tools_used: int = 0
    # Total MCP tool calls across all drill sessions (reporting; not dataplane)
    drills_used: int = 0
    # Down ports investigated once before the service digs (0 disables)
    max_port_investigations: int = 6
    port_investigations_used: int = 0
    port_tools_used: int = 0


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
    run_configuration: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    # Attributed conclusions (LLM/fallback). Not Evidence — cite evidence_ids.
    diagnoses: list[Diagnosis] = field(default_factory=list)
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
    # Routing faults, links gaining errors and IS-IS changes: when first seen and how often since,
    # by ``kind|subject``. Copied forward scan to scan; a fact about saved scans, not evidence.
    item_history: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Earlier scans' latest routing drill findings, by issue edge id, dated by ``run_id``: leads for
    # this scan's drills, not current evidence. Those on Issues this scan leaves alone are saved with it.
    previous_findings: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Earlier scans' latest concluded port findings, by ``if:device:port``, dated by ``run_id``. Not saved.
    previous_port_findings: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Those this scan kept for ports it did not investigate again, link ages counted on; saved with it.
    earlier_port_findings: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The last published scan's port counters, to subtract from this scan's. Not saved with this case.
    previous_port_counters: dict[str, Any] = field(default_factory=dict)
    # Token counts for the scan, and its cost and the budget when the provider reports them.
    llm_usage: dict[str, Any] = field(default_factory=dict)
    # MCP calls of the scan: all, from cache, and those made during LLM investigations.
    mcp_calls: dict[str, Any] = field(default_factory=dict)
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
    status: str,
    subject: dict[str, Any],
    fix_suggestion: str | None = None,
    confidence: str = "medium",
    complete: bool = True,
    verification_gap: dict[str, Any] | None = None,
    evidence_ids: list[str] | None = None,
) -> str:
    """Append an attributed diagnosis (not Evidence) in the diagnosis shape. Citations via evidence_ids."""
    case._diagnosis_seq += 1
    diag_id = f"dx_{case._diagnosis_seq}"
    # Fields in the order saved scans have always written them.
    rec: dict[str, Any] = {
        "id": diag_id,
        "kind": kind,
        "source": source,
        "observed": observed,
        "cause": cause,
        "evidence_ids": list(evidence_ids or []),
        "status": status,
        "subject": {"service_type": subject.get("service_type"), "name": subject["name"]},
    }
    if fix_suggestion is not None:
        rec["fix_suggestion"] = fix_suggestion
    rec["confidence"] = confidence
    rec["complete"] = complete
    if verification_gap is not None:
        rec["verification_gap"] = verification_gap
    case.diagnoses.append(rec)  # type: ignore[arg-type]
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
    rec: Issue = {
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
    account: Literal["drill", "dataplane", "port"] = "drill",
) -> bool:
    """Debit drill or dataplane tool call(s). Prefer a per-issue ``session`` cap."""
    b = case.budget
    if session is not None:
        if session.tools_used + n > session.max_tools:
            return False
        session.tools_used += n
        if account == "dataplane":
            b.dataplane_tools_used += n
        elif account == "port":
            b.port_tools_used += n
        else:
            b.drills_used += n
        return True
    # No session: global cap = issues × tools-per-issue (drill pool only)
    if account != "drill":
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
