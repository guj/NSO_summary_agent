"""Compare two diagnostic_mas case.json artifacts (run-to-run delta)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Issue statuses that still count as an unresolved problem.
_PROBLEM_STATUSES = frozenset(
    {"open", "needs_human", "budget_exhausted", "escalated"}
)

# Coverage values that mean the service was in scope / checked this run.
_CHECKED_COVERAGE = frozenset(
    {
        "basic_passed",
        "needs_investigation",
        "investigated",
        "unresolved",
        "budget_skipped",
        "llm_budget_exceeded",
        "collection_concluded",
        "category_peer_skipped",
    }
)

_FAULT_DP = frozenset({"down", "degraded"})
_OK_DP = frozenset({"up", "ok"})


def load_case_dict(path: Path | str) -> dict[str, Any]:
    """Load case.json from a run directory or a case.json file path."""
    p = Path(path)
    if p.is_dir():
        candidate = p / "case.json"
        if not candidate.is_file():
            raise FileNotFoundError(f"no case.json under {p}")
        p = candidate
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"case.json is not an object: {p}")
    return data


def load_previous_case(
    state_dir: Path | str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Load the last published case from ``latest.json`` under state_dir.

    Returns ``(case_dict, run_id)`` or ``(None, None)`` when unavailable.
    """
    state_dir = Path(state_dir)
    latest = state_dir / "latest.json"
    if not latest.is_file():
        return None, None
    try:
        meta = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(meta, dict):
        return None, None
    case_path = meta.get("case_path")
    run_id = meta.get("run_id")
    if not isinstance(case_path, str) or not case_path.strip():
        return None, None
    path = Path(case_path)
    if not path.is_file():
        return None, None
    try:
        return load_case_dict(path), str(run_id) if run_id else path.parent.name
    except (OSError, ValueError, json.JSONDecodeError):
        return None, None


def issue_key(issue: dict[str, Any]) -> str:
    """Stable identity for matching issues across runs."""
    layer = str(issue.get("layer") or "")
    code = str(issue.get("code") or "")
    edge = issue.get("edge_id")
    if isinstance(edge, str) and edge.strip():
        return f"{layer}|{code}|{edge.strip()}"
    devices = issue.get("devices")
    if isinstance(devices, list) and devices:
        devs = ",".join(sorted(str(d) for d in devices))
        return f"{layer}|{code}|devices:{devs}"
    msg = str(issue.get("message") or "")[:80]
    return f"{layer}|{code}|msg:{msg}"


def is_problem(issue: dict[str, Any]) -> bool:
    status = str(issue.get("status") or "open").lower()
    return status in _PROBLEM_STATUSES


def problem_map(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for issue in case.get("issues") or []:
        if not isinstance(issue, dict) or not is_problem(issue):
            continue
        out[issue_key(issue)] = issue
    return out


def coverage_map(case: dict[str, Any]) -> dict[str, str]:
    raw = case.get("service_coverage") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _issue_subject(issue: dict[str, Any]) -> str:
    edge = issue.get("edge_id")
    if isinstance(edge, str) and edge.strip():
        return edge.strip()
    devices = issue.get("devices")
    if isinstance(devices, list) and devices:
        return ",".join(str(d) for d in devices)
    return str(issue.get("code") or "unknown")


def _issue_line(issue: dict[str, Any]) -> str:
    subject = _issue_subject(issue)
    code = str(issue.get("code") or "?")
    status = str(issue.get("status") or "?")
    msg = str(issue.get("message") or "").strip()
    layer = str(issue.get("layer") or "")
    head = f"{subject} [{code}] ({status})"
    if layer:
        head = f"{subject} [{layer}/{code}] ({status})"
    if msg:
        return f"{head}: {msg}"
    return head


def _subject_in_coverage(subject: str, coverage: dict[str, str]) -> bool:
    if not coverage:
        return True  # unknown scope — do not treat as out-of-scope
    if subject in coverage:
        return True
    # device-keyed problems: any coverage is unrelated; treat as in-scope
    if subject.startswith("devices:") or "," in subject:
        return True
    return False


def _quarantined_devices(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """device_live_unreachable issues keyed by device name."""
    out: dict[str, dict[str, Any]] = {}
    for issue in case.get("issues") or []:
        if not isinstance(issue, dict) or not is_problem(issue):
            continue
        if str(issue.get("code") or "") != "device_live_unreachable":
            continue
        devices = issue.get("devices") or []
        if isinstance(devices, list) and devices:
            name = str(devices[0]).strip()
        else:
            name = _issue_subject(issue)
        if name:
            out[name] = issue
    return out


def _service_dataplane_map(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map service name → {status, complete, service_type} from diagnoses/evidence."""
    out: dict[str, dict[str, Any]] = {}

    def _put(
        name: str,
        *,
        status: str,
        complete: bool = True,
        service_type: str | None = None,
    ) -> None:
        key = str(name or "").strip()
        if not key:
            return
        st = str(status or "").lower()
        if not st:
            return
        prev = out.get(key)
        # Prefer incomplete / fault over a later ok when both exist.
        if prev is not None:
            if prev.get("complete") is False and complete:
                return
            if str(prev.get("status") or "") in _FAULT_DP and st in _OK_DP:
                return
        row: dict[str, Any] = {"status": st, "complete": complete}
        if service_type:
            row["service_type"] = service_type
        out[key] = row

    for dx in case.get("diagnoses") or []:
        if not isinstance(dx, dict):
            continue
        if str(dx.get("kind") or "") != "dataplane":
            continue
        subject = dx.get("subject") if isinstance(dx.get("subject"), dict) else {}
        name = subject.get("name") or dx.get("name")
        if not name:
            continue
        status = dx.get("status") or dx.get("dataplane_status") or ""
        complete = dx.get("complete", True)
        _put(
            str(name),
            status=str(status),
            complete=False if complete is False else True,
            service_type=str(subject.get("service_type") or "") or None,
        )

    for ev in case.get("evidence") or []:
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("kind") or "")
        if kind not in {"dataplane_finding", "dataplane_incomplete"}:
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        name = payload.get("name") or payload.get("service_name")
        if not name:
            continue
        status = payload.get("dataplane_status") or ""
        complete = kind != "dataplane_incomplete" and payload.get("complete", True)
        _put(
            str(name),
            status=str(status),
            complete=False if complete is False else True,
            service_type=str(payload.get("service_type") or "") or None,
        )
    return out


def _service_issue_faults(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Service-layer problem issues keyed by edge_id / subject."""
    out: dict[str, dict[str, Any]] = {}
    for issue in case.get("issues") or []:
        if not isinstance(issue, dict) or not is_problem(issue):
            continue
        if str(issue.get("layer") or "") != "services":
            continue
        subject = _issue_subject(issue)
        if subject:
            out[subject] = issue
    return out



def service_fault_history(
    older: dict[str, Any], newer: dict[str, Any],
    *, previous_run_id: str | None = None, run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Carry unresolved faults through sampling gaps without asserting current health.

    Only a complete current dataplane pass clears a historical fault.
    Baseline sync, missing inventory, and incomplete digs cannot clear it.
    """
    history = {
        str(row["service"]): dict(row)
        for row in older.get("last_known_service_faults") or []
        if isinstance(row, dict) and row.get("service")
    }
    for snapshot, observed_run in (
        (older, previous_run_id or older.get("run_id")),
        (newer, run_id or newer.get("run_id")),
    ):
        dp = _service_dataplane_map(snapshot)
        # Include collection-reported faults, but prefer completed diagnostics.
        findings = {}
        for name, issue in _service_issue_faults(snapshot).items():
            code = issue.get("code")
            if code in {"service_down", "service_degraded"}:
                findings[name] = {
                    "status": code.removeprefix("service_"),
                    "complete": True,
                    "cause": issue.get("message", ""),
                    "source": "collection",
                }
        findings.update(dp)
        for name, finding in findings.items():
            st = finding.get("status")
            if finding.get("complete") is False:
                continue
            if st in _OK_DP:
                history.pop(name, None)
            elif st in _FAULT_DP:
                diagnosis = next((
                    d for d in snapshot.get("diagnoses") or []
                    if d.get("kind") == "dataplane"
                    and (d.get("subject") or {}).get("name") == name
                    and d.get("status", d.get("dataplane_status")) == st
                ), {})
                history[name] = {
                    "service": name, "status": st,
                    "service_type": finding.get("service_type", ""),
                    "last_observed_run_id": observed_run or "unknown",
                    "cause": diagnosis.get("cause") or finding.get("cause") or "",
                    "observed": diagnosis.get("observed") or "",
                    "source": diagnosis.get("source") or finding.get("source") or "recorded finding",
                    "evidence_ids": list(diagnosis.get("evidence_ids") or []),
                }
    current_dp = _service_dataplane_map(newer)
    current_issues = _service_issue_faults(newer)
    for name, row in history.items():
        current = current_dp.get(name, {})
        confirmed = (
            current.get("status") in _FAULT_DP
            and current.get("complete") is not False
        ) or (
            not current and current_issues.get(name, {}).get("code")
            in {"service_down", "service_degraded"}
        )
        row["rechecked"] = bool(confirmed)
        row["verification"] = (
            "fault observed this run" if confirmed else
            "recheck inconclusive; last-known fault unresolved" if current else
            "not rechecked; last-known fault unresolved"
        )
    return sorted(history.values(), key=lambda r: r["service"])


def format_last_known_faults(rows: list[dict[str, Any]]) -> list[str]:
    pending = [r for r in rows if not r.get("rechecked")]
    if not pending:
        return []
    lines = ["**Last-known service faults — not cleared:**", ""]
    for row in pending:
        lines.append(
            f"- `{row['service']}` — last known {row['status']} "
            f"(run `{row.get('last_observed_run_id', 'unknown')}`); "
            f"{row.get('verification', 'not rechecked')}. "
            f"{row.get('cause') or ''}".rstrip()
        )
    lines.append("")
    return lines

def compute_case_delta(
    older: dict[str, Any], newer: dict[str, Any]
) -> dict[str, Any]:
    """Diff two case dicts into operational change buckets.

    Buckets operators care about most:
    - newly failed services
    - recovered devices
    - newly missing evidence
    - persistent problems
    Plus legacy issue lists (new_problems / recovered / out_of_scope) and
    coverage_changes for the CLI.
    """
    old_probs = problem_map(older)
    new_probs = problem_map(newer)
    old_cov = coverage_map(older)
    new_cov = coverage_map(newer)
    old_q = _quarantined_devices(older)
    new_q = _quarantined_devices(newer)
    live_verified = set(newer.get("live_verified_devices") or [])
    old_dp = _service_dataplane_map(older)
    new_dp = _service_dataplane_map(newer)
    old_svc_faults = _service_issue_faults(older)
    history = service_fault_history(older, newer)
    old_history = {r["service"]: r for r in service_fault_history(older, {})}
    pending_names = {r["service"] for r in history if not r["rechecked"]}
    new_svc_faults = _service_issue_faults(newer)

    new_problems: list[dict[str, Any]] = []
    recovered: list[dict[str, Any]] = []
    persistent: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []

    for key, issue in new_probs.items():
        if key not in old_probs:
            new_problems.append(issue)
        else:
            persistent.append(issue)

    for key, issue in old_probs.items():
        if key in new_probs:
            continue
        subject = _issue_subject(issue)
        if issue.get("code") == "device_live_unreachable":
            devices = set(issue.get("devices") or [])
            if devices and devices <= live_verified and not devices.intersection(new_q):
                recovered.append(issue)
            else:
                out_of_scope.append(issue)
            continue
        # A baseline pass or an omitted service is not dataplane recovery.
        if str(issue.get("layer") or "") == "services" and subject in pending_names:
            out_of_scope.append(issue)
            continue
        # Narrower newer run: missing from newer coverage → not a recovery.
        if new_cov and not _subject_in_coverage(subject, new_cov):
            out_of_scope.append(issue)
        else:
            recovered.append(issue)

    coverage_changes: list[dict[str, str]] = []
    newly_missing_evidence: list[dict[str, str]] = []
    for name, old_label in old_cov.items():
        if name not in new_cov:
            row = {
                "service": name,
                "from": old_label,
                "to": "not_checked",
                "kind": "not_checked",
            }
            coverage_changes.append(row)
            if old_label in _CHECKED_COVERAGE:
                newly_missing_evidence.append(
                    {
                        "kind": "coverage_gap",
                        "subject": name,
                        "detail": f"{old_label} → not_checked",
                    }
                )
        elif old_label in _CHECKED_COVERAGE and new_cov[name] == "budget_skipped":
            if old_label != "budget_skipped":
                row = {
                    "service": name,
                    "from": old_label,
                    "to": "budget_skipped",
                    "kind": "budget_skipped",
                }
                coverage_changes.append(row)
                newly_missing_evidence.append(
                    {
                        "kind": "coverage_gap",
                        "subject": name,
                        "detail": f"{old_label} → budget_skipped",
                    }
                )
        elif old_label != new_cov[name]:
            coverage_changes.append(
                {
                    "service": name,
                    "from": old_label,
                    "to": new_cov[name],
                    "kind": "label_change",
                }
            )

    # Digs that newly failed to conclude = missing evidence.
    for name, row in new_dp.items():
        if row.get("complete") is not False:
            continue
        prev = old_dp.get(name)
        if prev is None or prev.get("complete") is not False:
            newly_missing_evidence.append(
                {
                    "kind": "incomplete_dig",
                    "subject": name,
                    "detail": "dataplane verification incomplete",
                }
            )

    # Newly quarantined devices = missing live evidence this run.
    for device, issue in new_q.items():
        if device in old_q:
            continue
        msg = str(issue.get("message") or "").strip()
        newly_missing_evidence.append(
            {
                "kind": "collection_gap",
                "subject": device,
                "detail": msg or "live MCP collection failed",
            }
        )

    recovered_devices = sorted(d for d in old_q if d not in new_q and d in live_verified)

    newly_failed_services: list[dict[str, str]] = []
    seen_failed: set[str] = set()

    def _add_failed(
        name: str,
        *,
        status: str,
        detail: str,
        service_type: str | None = None,
    ) -> None:
        key = str(name).strip()
        if not key or key in seen_failed:
            return
        seen_failed.add(key)
        row: dict[str, str] = {
            "service": key,
            "status": status,
            "detail": detail,
            "classification": "newly_detected_fault",
            "regression_proven": False,
        }
        if service_type:
            row["service_type"] = service_type
        newly_failed_services.append(row)

    for name, row in new_dp.items():
        st = str(row.get("status") or "").lower()
        if st not in _FAULT_DP:
            continue
        prev = old_history.get(name) or old_dp.get(name)
        prev_st = str((prev or {}).get("status") or "").lower()
        if prev is None or prev_st in _OK_DP or prev_st in {"", "unknown", "not_checked"}:
            _add_failed(
                name,
                status=st,
                detail=(
                    f"dataplane {prev_st or 'unchecked'} → {st}"
                    if prev is not None
                    else f"dataplane={st} (new)"
                ),
                service_type=str(row.get("service_type") or "") or None,
            )
        elif prev_st != st and prev_st in _FAULT_DP:
            # severity change still operationally new
            _add_failed(
                name,
                status=st,
                detail=f"dataplane {prev_st} → {st}",
                service_type=str(row.get("service_type") or "") or None,
            )

    for name, issue in new_svc_faults.items():
        if name in seen_failed:
            continue
        if name in old_svc_faults or name in old_history:
            continue
        # Skip pure collection/sync unknowns that are not service faults.
        code = str(issue.get("code") or "")
        if code in {"service_unknown", "service_sync_unknown"}:
            continue
        _add_failed(
            name,
            status=str(issue.get("status") or "open"),
            detail=str(issue.get("message") or code or "new service problem"),
        )

    focus_old = list(older.get("focus_devices") or [])
    focus_new = list(newer.get("focus_devices") or [])
    notes: list[str] = []
    if focus_old != focus_new and (focus_old or focus_new):
        notes.append(
            "focus_devices differ: "
            f"{focus_old or '(none)'} → {focus_new or '(none)'}"
        )
    if old_cov and not new_cov:
        notes.append(
            "newer case has no service_coverage "
            "(older runs may predate coverage persistence)"
        )
    if new_cov and not old_cov:
        notes.append(
            "older case has no service_coverage "
            "(older runs may predate coverage persistence)"
        )

    return {
        "new_problems": new_problems,
        "recovered": recovered,
        "persistent": persistent,
        "out_of_scope": out_of_scope,
        "coverage_changes": coverage_changes,
        # Keep the legacy JSON key for consumers; no claim of failure onset.
        "newly_failed_services": newly_failed_services,
        "last_known_service_faults": history,
        "recovered_devices": recovered_devices,
        "newly_missing_evidence": newly_missing_evidence,
        "notes": notes,
    }


def delta_has_operational_changes(delta: dict[str, Any]) -> bool:
    """True when any operator-priority bucket is non-empty."""
    if delta.get("last_known_service_faults"):
        return True
    if delta.get("newly_failed_services"):
        return True
    if delta.get("recovered_devices"):
        return True
    if delta.get("newly_missing_evidence"):
        return True
    if delta.get("persistent"):
        return True
    if delta.get("new_problems") or delta.get("recovered"):
        return True
    if delta.get("coverage_changes"):
        return True
    return False


def format_case_delta(
    delta: dict[str, Any],
    *,
    older_label: str = "older",
    newer_label: str = "newer",
) -> str:
    """Full CLI delta report (``nso-diagnostic-delta``)."""
    lines = [
        f"# Diagnostic delta ({older_label} → {newer_label})",
        "",
    ]
    for note in delta.get("notes") or []:
        lines.append(f"_Note: {note}_")
        lines.append("")

    def _section(title: str, items: list[Any], empty: str) -> None:
        lines.append(f"## {title}")
        if not items:
            lines.append(empty)
        else:
            for item in items:
                if isinstance(item, dict) and "service" in item and "from" in item:
                    lines.append(
                        f"- {item['service']}: {item['from']} → {item['to']}"
                    )
                elif isinstance(item, dict) and item.get("kind") in {
                    "coverage_gap",
                    "incomplete_dig",
                    "collection_gap",
                }:
                    lines.append(
                        f"- {item.get('subject')}: {item.get('detail')}"
                    )
                elif isinstance(item, dict) and "service" in item:
                    st = item.get("status") or "?"
                    detail = item.get("detail") or ""
                    bit = f"- {item['service']} ({st})"
                    if detail:
                        bit += f": {detail}"
                    lines.append(bit)
                elif isinstance(item, dict):
                    lines.append(f"- {_issue_line(item)}")
                else:
                    lines.append(f"- {item}")
        lines.append("")

    _section(
        "Newly detected service faults",
        list(delta.get("newly_failed_services") or []),
        "None",
    )
    _section(
        "Recovered devices",
        list(delta.get("recovered_devices") or []),
        "None",
    )
    _section(
        "Newly missing evidence",
        list(delta.get("newly_missing_evidence") or []),
        "None",
    )
    _section(
        "New problems",
        list(delta.get("new_problems") or []),
        "None",
    )
    _section(
        "Recovered problems",
        list(delta.get("recovered") or []),
        "None",
    )
    _section(
        "Persistent problems",
        list(delta.get("persistent") or []),
        "None",
    )
    _section(
        "Coverage changes",
        list(delta.get("coverage_changes") or []),
        "None",
    )
    out_of_scope = list(delta.get("out_of_scope") or [])
    if out_of_scope:
        _section(
            "Out of scope (not in newer coverage; not counted as recovered)",
            out_of_scope,
            "None",
        )
    lines.extend(format_last_known_faults(list(delta.get("last_known_service_faults") or [])))
    return "\n".join(lines).rstrip() + "\n"


def format_changes_since_previous(
    delta: dict[str, Any] | None,
    *,
    previous_run_id: str | None = None,
    persistent_limit: int = 8,
) -> list[str]:
    """Compact operator section for the diagnostic report.

    Emphasizes new failures, recovered devices, newly missing evidence, and
    persistent problems — not unchanged PE-side observations.
    """
    lines = ["## Changes since previous run", ""]
    if delta is None:
        lines.append(
            "First published baseline unavailable — no previous snapshot to compare."
        )
        return lines

    if previous_run_id:
        lines.append(f"_Compared to `{previous_run_id}`._")
        lines.append("")

    for note in delta.get("notes") or []:
        lines.append(f"_Note: {note}_")
        lines.append("")

    failed = list(delta.get("newly_failed_services") or [])
    recovered_devs = list(delta.get("recovered_devices") or [])
    missing = list(delta.get("newly_missing_evidence") or [])
    persistent = list(delta.get("persistent") or [])
    # Recovered problems that are not already covered by recovered_devices.
    recovered_probs = [
        i
        for i in (delta.get("recovered") or [])
        if isinstance(i, dict)
        and str(i.get("code") or "") != "device_live_unreachable"
    ]

    if not delta_has_operational_changes(delta) and not recovered_probs:
        lines.append(
            "No material changes — no newly detected service faults, recovered devices, "
            "newly missing evidence, or persistent open problems versus the "
            "previous run."
        )
        return lines

    lines.append("**Newly detected service faults:**")
    if not failed:
        lines.append("- None")
    else:
        for item in failed:
            name = item.get("service") or "?"
            st = item.get("status") or "?"
            detail = item.get("detail") or ""
            stype = item.get("service_type")
            label = f"`{stype}/{name}`" if stype else f"`{name}`"
            bit = f"- {label} — {st}"
            if detail:
                bit += f" ({detail})"
            lines.append(bit)
    lines.append("")

    if failed:
        lines.append("A newly detected fault does not prove a regression: prior "
                     "readiness may have used different checks. Failure onset is unverified.")
        lines.append("")
    lines.extend(format_last_known_faults(list(delta.get("last_known_service_faults") or [])))
    lines.append("**Recovered devices:**")
    if not recovered_devs and not recovered_probs:
        lines.append("- None")
    else:
        for device in recovered_devs:
            lines.append(
                f"- `{device}` — a current-run live query succeeded "
                "(prior `device_live_unreachable` cleared)"
            )
        for issue in recovered_probs[:persistent_limit]:
            lines.append(f"- {_issue_line(issue)}")
        extra = len(recovered_probs) - persistent_limit
        if extra > 0:
            lines.append(f"- …and {extra} more recovered problem(s)")
    lines.append("")

    lines.append("**Newly missing evidence:**")
    if not missing:
        lines.append("- None")
    else:
        for item in missing:
            subj = item.get("subject") or "?"
            detail = item.get("detail") or item.get("kind") or "gap"
            kind = item.get("kind") or "gap"
            lines.append(f"- `{subj}` — {detail} ({kind})")
    lines.append("")

    lines.append("**Persistent problems:**")
    if not persistent:
        lines.append("- None")
    else:
        for issue in persistent[:persistent_limit]:
            lines.append(f"- {_issue_line(issue)}")
        extra = len(persistent) - persistent_limit
        if extra > 0:
            lines.append(
                f"- …and {extra} more unchanged open problem(s) "
                "(see Devices / Services; not re-listed here)"
            )
        elif len(persistent) > 1:
            lines.append(
                "_Unchanged open items — details under Devices / Services; "
                "not re-summarized as new findings._"
            )
    return lines


def compare_run_dirs(older: Path | str, newer: Path | str) -> str:
    older_p = Path(older)
    newer_p = Path(newer)
    delta = compute_case_delta(load_case_dict(older_p), load_case_dict(newer_p))
    return format_case_delta(
        delta,
        older_label=older_p.name,
        newer_label=newer_p.name,
    )


def compact_delta_for_llm(delta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Small delta payload for the summary LLM (no full issue dumps)."""
    if delta is None:
        return None

    def _trim_issues(items: list[Any], limit: int = 12) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for issue in items[:limit]:
            if not isinstance(issue, dict):
                continue
            out.append(
                {
                    "code": issue.get("code"),
                    "layer": issue.get("layer"),
                    "edge_id": issue.get("edge_id"),
                    "devices": issue.get("devices"),
                    "message": str(issue.get("message") or "")[:160],
                    "status": issue.get("status"),
                }
            )
        return out

    return {
        "newly_failed_services": list(delta.get("newly_failed_services") or [])[:20],
        "recovered_devices": list(delta.get("recovered_devices") or [])[:20],
        "newly_missing_evidence": list(delta.get("newly_missing_evidence") or [])[
            :20
        ],
        "last_known_service_faults": list(delta.get("last_known_service_faults") or [])[:20],
        "regression_policy": "No proven regression from status changes alone; comparable checks are required. Failure onset is unverified.",
        "persistent_count": len(delta.get("persistent") or []),
        "persistent_sample": _trim_issues(list(delta.get("persistent") or [])),
        "recovered_problem_count": len(delta.get("recovered") or []),
        "new_problem_count": len(delta.get("new_problems") or []),
    }
