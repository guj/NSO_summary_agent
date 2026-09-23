from diagnostic_mas.case import (
    Budget,
    CaseFile,
    add_diagnosis,
    add_evidence,
    open_issue,
)
from diagnostic_mas.roles.summary import (
    _compact_case_for_llm,
    dataplane_tally_for_summary,
)


def test_compact_case_includes_live_l2_and_devices():
    case = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    eid = add_evidence(
        case,
        {"kind": "spine", "role": "service", "layer": "services", "payload": {}},
    )
    open_issue(
        case,
        code="service_degraded",
        message="l2ptp bad: degraded — lbnl Hu0/0/0/17.100 AC UP; renc Hu0/0/0/0.100 AC DN",
        evidence_ids=[eid],
        layer="services",
        edge_id="bad",
        devices=["lbnl-data-sw", "renc-data-sw"],
        live_l2={
            "summary": "degraded",
            "endpoints": [
                {"device": "lbnl-data-sw", "ac": "Hu0/0/0/17.100", "st": "UP"},
                {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "DN"},
            ],
        },
        in_sync=None,
        device_sync={"lbnl-data-sw": "in-sync", "renc-data-sw": "in-sync"},
    )
    compact = _compact_case_for_llm(case)
    issue = compact["issues"][0]
    assert issue["devices"] == ["lbnl-data-sw", "renc-data-sw"]
    assert issue["live_l2"]["summary"] == "degraded"
    assert issue["live_l2"]["endpoints"][1]["st"] == "DN"
    assert issue["device_sync"]["renc-data-sw"] == "in-sync"
    assert "in_sync" in issue


def test_compact_includes_drill_payload_fields():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=2))
    add_evidence(
        case,
        {
            "kind": "drill",
            "role": "drill",
            "layer": "services",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "renc-data-sw",
                    "input_command": "interfaces Hu0/0/0/0.100",
                },
                "result": {"admin": "up", "oper": "down"},
            },
        },
    )
    compact = _compact_case_for_llm(case)
    drill = next(e for e in compact["evidence"] if e.get("kind") == "drill")
    assert drill["check"] == "exec_show"
    assert "Hu0/0/0/0.100" in str(drill.get("args"))


def test_compact_includes_dataplane_budget_per_dig_semantics():
    case = CaseFile(
        budget=Budget(
            max_deep_checks=0,
            max_handoffs=0,
            max_dataplane_tools=40,
        )
    )
    case.budget.dataplane_tools_used = 302
    for i in range(3):
        add_diagnosis(
            case,
            kind="dataplane",
            source="llm",
            status="up",
            subject={"name": f"svc-{i}", "service_type": "l3rt"},
            cause="ok",
            observed="ok",
            extra={"complete": True},
        )
    compact = _compact_case_for_llm(case)
    bud = compact["dataplane_budget"]
    assert bud["tools_used_total"] == 302
    assert bud["max_tools_per_dig"] == 40
    assert bud["digs"] == 3
    assert "per-dig" in bud["note"].lower()
    assert "not a fleet total" in bud["note"].lower()


def test_run_details_dataplane_tools_are_per_dig():
    from diagnostic_mas.operator_report import format_run_details

    case = CaseFile(
        budget=Budget(
            max_deep_checks=0,
            max_handoffs=0,
            max_dataplane_tools=40,
        )
    )
    case.budget.dataplane_tools_used = 302
    for i in range(24):
        add_diagnosis(
            case,
            kind="dataplane",
            source="llm",
            status="up",
            subject={"name": f"svc-{i}", "service_type": "l2sts"},
            cause="c",
            observed="o",
        )
    text = "\n".join(format_run_details(case))
    assert "302 used across 24 digs" in text
    assert "40 allowed per dig" in text
    assert "not a fleet total" in text
    assert "against a 40 cap" not in text


def test_system_prompt_selects_pinned():
    from diagnostic_mas.roles.summary import _system_prompt_for_summary

    pinned = _system_prompt_for_summary(pinned=True)
    normal = _system_prompt_for_summary(pinned=False)
    assert "SHORT executive skim" in pinned
    assert "do not paste long observed" in pinned.lower()
    assert "dataplane=" in pinned
    assert "dataplane_tally" in pinned
    assert "incomplete_or_unresolved_by_type" in pinned
    assert "SHORT executive skim" in normal
    assert "dataplane_tally" in normal
    assert 'Do NOT emit "Suggested remedies (hypotheses)"' in normal
    assert "Pinned findings & fix steps" not in pinned


def test_dataplane_tally_counts_incomplete_by_type():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    names = [
        "L2STS_MAX_WASH-aaa",
        "L2STS_STAR_NCSA-bbb",
        "P4_KANS_NET-ccc",
        "fabric_network-5194f01c",
        "fabric_network-556ad07e",
        "fabric_network-cfb95d1e",
        "l2_ctrl-e36b",
        "l2_ctrl_ue_upf-1697",
        "l2_ctrl_ue_upf-99f1",
        "l2_data-3685",
    ]
    for name in names:
        add_diagnosis(
            case,
            kind="dataplane",
            source="llm",
            status="unknown",
            subject={"name": name, "service_type": "l2sts"},
            cause="verification incomplete",
            observed="gap",
            extra={"complete": False},
        )
    add_diagnosis(
        case,
        kind="dataplane",
        source="llm",
        status="up",
        subject={"name": "ATLA_SALT", "service_type": "l2ptp"},
        cause="PE-side ready",
        observed="xc up",
        extra={"complete": True},
    )
    tally = dataplane_tally_for_summary(case)
    assert tally["digs_total"] == 11
    assert tally["by_status"]["unknown"] == 10
    assert tally["by_status"]["up"] == 1
    assert tally["incomplete_or_unresolved_total"] == 10
    l2 = tally["incomplete_or_unresolved_by_type"]["l2sts"]
    assert l2["count"] == 10
    assert "l2_data-3685" in l2["names"]
    assert "l2_ctrl_ue_upf-1697" in l2["names"]
    compact = _compact_case_for_llm(case)
    assert compact["dataplane_tally"]["incomplete_or_unresolved_total"] == 10
    # All dataplane diagnoses retained (not truncated at 20).
    assert len([d for d in compact["diagnoses"] if d.get("kind") == "dataplane"]) == 11


def test_compact_keeps_all_dataplane_diagnoses_beyond_20():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    for i in range(24):
        add_diagnosis(
            case,
            kind="dataplane",
            source="llm",
            status="up" if i < 14 else "unknown",
            subject={
                "name": f"svc-{i}",
                "service_type": "l2sts" if i >= 14 else "l3rt",
            },
            cause="c",
            observed="o",
            extra={"complete": i < 14},
        )
    compact = _compact_case_for_llm(case)
    assert len(compact["diagnoses"]) == 24
    assert compact["dataplane_tally"]["incomplete_or_unresolved_total"] == 10
    assert compact["dataplane_tally"]["incomplete_or_unresolved_by_type"]["l2sts"][
        "count"
    ] == 10
