"""Validate and apply cross-role Handoff records."""

from __future__ import annotations

from typing import Any

from diagnostic_mas.case import CaseFile, debit_handoff, set_issue_status

ALLOWED_HANDOFF_TARGETS = frozenset({"isis", "bgp", "service", "device"})


def validate_handoff(
    case: CaseFile,
    handoff: dict[str, Any],
    allowlisted_actions: frozenset[str],
) -> tuple[bool, str]:
    to_role = str(handoff.get("to_role") or "")
    if to_role not in ALLOWED_HANDOFF_TARGETS:
        return False, f"invalid to_role: {to_role}"

    issue_ids = list(handoff.get("issue_ids") or [])
    if not issue_ids:
        return False, "handoff requires issue_ids"
    known = {i.get("id") for i in case.issues}
    for iid in issue_ids:
        if iid not in known:
            return False, f"unknown issue_id: {iid}"

    actions = list(handoff.get("suggested_actions") or [])
    for action in actions:
        if action not in allowlisted_actions:
            return False, f"action not on allowlist: {action}"

    cost = int(handoff.get("budget_cost") or 1)
    if cost < 1:
        return False, "budget_cost must be >= 1"
    remaining = case.budget.max_handoffs - case.budget.handoffs_used
    if cost > remaining:
        return False, "handoff budget exhausted"

    return True, ""


def apply_handoff(
    case: CaseFile,
    handoff: dict[str, Any],
    allowlisted_actions: frozenset[str],
) -> tuple[bool, str]:
    ok, err = validate_handoff(case, handoff, allowlisted_actions)
    if not ok:
        return False, err

    cost = int(handoff.get("budget_cost") or 1)
    if not debit_handoff(case, cost):
        return False, "handoff budget exhausted"

    rec = dict(handoff)
    if not rec.get("id"):
        rec["id"] = f"ho_{len(case.handoffs) + 1}"
    case.handoffs.append(rec)

    for iid in list(rec.get("issue_ids") or []):
        set_issue_status(case, str(iid), "escalated")

    return True, ""
