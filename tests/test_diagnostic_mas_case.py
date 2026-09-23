from diagnostic_mas.case import (
    Budget,
    CaseFile,
    add_evidence,
    debit_deep_check,
    debit_drill,
    debit_handoff,
    open_issue,
    set_issue_status,
    should_stop,
)


def test_should_stop_when_no_open_issues():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    eid = add_evidence(case, {"kind": "test", "payload": {}})
    iid = open_issue(case, code="x", message="m", evidence_ids=[eid])
    assert should_stop(case) is False
    set_issue_status(case, iid, "explained")
    assert should_stop(case) is True


def test_should_stop_when_both_budgets_exhausted():
    case = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    eid = add_evidence(case, {"kind": "t", "payload": {}})
    open_issue(case, code="x", message="m", evidence_ids=[eid])
    assert debit_deep_check(case) is True
    assert debit_handoff(case) is True
    assert should_stop(case) is True


def test_add_evidence_assigns_id():
    case = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    eid = add_evidence(
        case, {"kind": "spine", "layer": "underlay", "payload": {"edges": []}}
    )
    assert eid.startswith("ev_")
    assert case.evidence[0]["id"] == eid


def test_debit_drill_respects_max():
    from diagnostic_mas.case import DrillSession

    case = CaseFile(
        budget=Budget(
            max_deep_checks=5, max_handoffs=5, max_drill_issues=1, max_tools_per_drill=2
        )
    )
    session = DrillSession(max_tools=2)
    assert debit_drill(case, session=session) is True
    assert case.budget.drills_used == 1
    assert debit_drill(case, session=session) is True
    assert case.budget.drills_used == 2
    assert debit_drill(case, session=session) is False
    assert case.budget.drills_used == 2


def test_debit_dataplane_account_separate_from_drill():
    from diagnostic_mas.case import DrillSession

    case = CaseFile(
        budget=Budget(
            max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=2
        )
    )
    session = DrillSession(max_tools=40)
    assert debit_drill(case, session=session, account="dataplane") is True
    assert case.budget.dataplane_tools_used == 1
    assert case.budget.drills_used == 0
    assert debit_drill(case, session=session, account="drill") is True
    assert case.budget.dataplane_tools_used == 1
    assert case.budget.drills_used == 1


def test_budget_defaults_drill_split():
    b = Budget(max_deep_checks=1, max_handoffs=1)
    assert b.max_drill_issues == 2
    assert b.max_tools_per_drill == 12
    assert b.drill_issues_used == 0
    assert b.drills_used == 0
    assert b.dataplane_tools_used == 0
    assert b.max_dataplane_tools == 40


def test_add_diagnosis_assigns_id_and_cites_evidence():
    from diagnostic_mas.case import add_diagnosis, evidence_ids_after

    case = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    eid = add_evidence(case, {"kind": "drill", "payload": {"check": "x"}})
    watermark = 0
    ids = evidence_ids_after(case, watermark)
    assert eid in ids
    dx = add_diagnosis(
        case,
        kind="dataplane",
        source="llm",
        status="down",
        subject={"service_type": "l2sts", "name": "svc1"},
        observed="AC missing",
        cause="not in xconnect",
        evidence_ids=ids,
        confidence="high",
    )
    assert dx.startswith("dx_")
    assert len(case.diagnoses) == 1
    assert case.diagnoses[0]["evidence_ids"] == [eid]
    assert case.diagnoses[0]["status"] == "down"
    assert case.diagnoses[0]["subject"]["name"] == "svc1"
