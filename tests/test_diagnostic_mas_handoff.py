from diagnostic_mas.case import Budget, CaseFile, add_evidence, open_issue
from diagnostic_mas.handoff import apply_handoff


def test_reject_handoff_unknown_issue():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    ok, err = apply_handoff(
        case,
        {
            "from_role": "isis",
            "to_role": "device",
            "issue_ids": ["missing"],
            "reason": "check optics",
            "suggested_actions": ["get_hardware_health"],
            "budget_cost": 1,
        },
        frozenset({"get_hardware_health"}),
    )
    assert ok is False
    assert "issue" in err.lower()


def test_accept_handoff_and_debit():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=2))
    eid = add_evidence(case, {"kind": "t", "payload": {}})
    iid = open_issue(case, code="adj_down", message="x", evidence_ids=[eid])
    ok, _ = apply_handoff(
        case,
        {
            "from_role": "isis",
            "to_role": "device",
            "issue_ids": [iid],
            "reason": "local check",
            "suggested_actions": ["get_hardware_health"],
            "budget_cost": 1,
        },
        frozenset({"get_hardware_health", "exec_show"}),
    )
    assert ok is True
    assert case.budget.handoffs_used == 1
    assert case.issues[0]["status"] == "escalated"


def test_reject_non_allowlisted_action():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=2))
    eid = add_evidence(case, {"kind": "t", "payload": {}})
    iid = open_issue(case, code="x", message="m", evidence_ids=[eid])
    ok, err = apply_handoff(
        case,
        {
            "from_role": "bgp",
            "to_role": "device",
            "issue_ids": [iid],
            "reason": "bad",
            "suggested_actions": ["rm_rf"],
            "budget_cost": 1,
        },
        frozenset({"get_hardware_health"}),
    )
    assert ok is False
    assert "allowlist" in err.lower() or "action" in err.lower()
