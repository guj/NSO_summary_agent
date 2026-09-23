from unittest.mock import patch

import pytest

from diagnostic_mas.case import Budget, CaseFile, add_evidence, open_issue
from diagnostic_mas.coordinator import run_autonomous_loop
from diagnostic_mas.planner import parse_planner_response, plan_and_gate_for_issue
from diagnostic_mas.report import render_report
from diagnostic_mas.roles.summary import summary_narrative


def _seed_underlay_issue(case: CaseFile) -> str:
    eid = add_evidence(
        case,
        {
            "kind": "spine",
            "role": "isis",
            "layer": "underlay",
            "payload": {
                "operational_summary": {"up": 0, "down": 1},
                "static_summary": {},
            },
        },
    )
    return open_issue(
        case,
        code="adjacency_down",
        message="renc-data-sw↔lbnl-data-sw down",
        evidence_ids=[eid],
        layer="underlay",
        edge_id="renc-data-sw|lbnl-data-sw",
        devices=["renc-data-sw", "lbnl-data-sw"],
    )


def test_parse_planner_response_object_and_array():
    obj = parse_planner_response(
        '{"plans":[{"check":"exec_show"}],"handoffs":[{"to_role":"device"}],'
        '"hypotheses":[{"text":"fiber?"}]}'
    )
    assert len(obj["plans"]) == 1
    assert len(obj["handoffs"]) == 1
    assert obj["hypotheses"][0]["text"] == "fiber?"
    arr = parse_planner_response('[{"check":"exec_show","args":{}}]')
    assert len(arr["plans"]) == 1
    assert arr["handoffs"] == []


def test_gate_drops_invented_device_no_evidence_from_model():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    _seed_underlay_issue(case)
    issue = case.issues[0]

    def fake_llm(_settings, **_kwargs):
        return [
            {
                "check": "exec_show",
                "args": {"device": "totally-fake-sw", "command": "isis neighbors"},
                "reason": "invented",
            }
        ]

    class S:
        fabric_model = "x"
        fabric_api_key = "k"

    role, gated = plan_and_gate_for_issue(
        S(),  # type: ignore[arg-type]
        case,
        issue,
        device_names={"renc-data-sw", "lbnl-data-sw"},
        llm_plan_fn=fake_llm,
    )
    assert role == "isis"
    assert gated == []
    assert all(e.get("kind") != "deep_check" for e in case.evidence)


@pytest.mark.asyncio
async def test_llm_deep_check_writes_evidence_and_debits():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    _seed_underlay_issue(case)

    def fake_llm(_settings, **_kwargs):
        return [
            {
                "check": "exec_show",
                "args": {"device": "renc-data-sw", "command": "isis neighbors"},
                "reason": "check adj",
                "issue_id": "renc-data-sw|lbnl-data-sw",
            }
        ]

    async def fake_checks(_client, role, plan):
        assert role == "isis"
        assert len(plan) == 1
        return [{"check": "exec_show", "ok": True, "snippet": "UP"}]

    with patch(
        "diagnostic_mas.coordinator.run_role_deep_checks", new=fake_checks
    ):
        await run_autonomous_loop(
            object(),
            object(),  # type: ignore[arg-type]
            case,
            device_names=["renc-data-sw", "lbnl-data-sw"],
            llm_plan_fn=fake_llm,
        )

    assert case.budget.deep_checks_used == 1
    deep = [e for e in case.evidence if e.get("kind") == "deep_check"]
    assert len(deep) == 1
    assert case.issues[0]["status"] == "explained"


@pytest.mark.asyncio
async def test_llm_error_only_deep_checks_do_not_mark_explained():
    """Nonempty results that are only errors must not count as success."""
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=0))
    _seed_underlay_issue(case)

    def fake_llm(_settings, **_kwargs):
        return [
            {
                "check": "exec_show",
                "args": {"device": "renc-data-sw", "command": "isis neighbors"},
                "reason": "check adj",
            }
        ]

    async def fake_checks(_client, _role, plan):
        return [
            {
                "check": t["check"],
                "args": t["args"],
                "error": "MCP timeout",
            }
            for t in plan
        ]

    with patch(
        "diagnostic_mas.coordinator.run_role_deep_checks", new=fake_checks
    ):
        await run_autonomous_loop(
            object(),
            object(),  # type: ignore[arg-type]
            case,
            device_names=["renc-data-sw", "lbnl-data-sw"],
            llm_plan_fn=fake_llm,
        )

    assert case.budget.deep_checks_used == 1
    deep = [e for e in case.evidence if e.get("kind") == "deep_check"]
    assert len(deep) == 1
    assert case.issues[0]["status"] == "needs_human"


@pytest.mark.asyncio
async def test_llm_empty_gated_plan_marks_needs_human():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    eid = add_evidence(
        case,
        {
            "kind": "spine",
            "role": "isis",
            "layer": "underlay",
            "payload": {"operational_summary": {}, "static_summary": {}},
        },
    )
    open_issue(
        case,
        code="adjacency_down",
        message="unknown failure",
        evidence_ids=[eid],
        layer="underlay",
    )

    def fake_llm(_settings, **_kwargs):
        return []

    await run_autonomous_loop(
        object(),
        object(),  # type: ignore[arg-type]
        case,
        device_names=["renc-data-sw"],
        llm_plan_fn=fake_llm,
    )
    assert case.issues[0]["status"] == "needs_human"
    assert case.budget.deep_checks_used == 0


@pytest.mark.asyncio
async def test_llm_respects_deep_check_budget():
    case = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=0))
    _seed_underlay_issue(case)

    def fake_llm(_settings, **_kwargs):
        return [
            {
                "check": "exec_show",
                "args": {"device": "renc-data-sw", "command": "isis neighbors"},
                "reason": "a",
            },
            {
                "check": "exec_show",
                "args": {"device": "lbnl-data-sw", "command": "isis neighbors"},
                "reason": "b",
            },
        ]

    async def fake_checks(_client, _role, plan):
        return [{"check": t["check"], "args": t["args"]} for t in plan]

    with patch(
        "diagnostic_mas.coordinator.run_role_deep_checks", new=fake_checks
    ):
        await run_autonomous_loop(
            object(),
            object(),  # type: ignore[arg-type]
            case,
            device_names=["renc-data-sw", "lbnl-data-sw"],
            llm_plan_fn=fake_llm,
        )

    assert case.budget.deep_checks_used == 1
    deep = [e for e in case.evidence if e.get("kind") == "deep_check"]
    assert len(deep[0]["payload"]["plan"]) == 1


@pytest.mark.asyncio
async def test_llm_handoff_to_device_records_hypothesis():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    iid = _seed_underlay_issue(case)

    def fake_turn(_settings, **_kwargs):
        return {
            "plans": [],
            "handoffs": [
                {
                    "to_role": "device",
                    "issue_ids": [iid],
                    "reason": "check optics",
                    "suggested_actions": ["get_hardware_health"],
                    "budget_cost": 1,
                    "devices": ["renc-data-sw"],
                }
            ],
            "hypotheses": [{"text": "possible optic fault", "issue_ids": [iid]}],
        }

    async def fake_followup(_client, case_obj, handoff):
        add_evidence(
            case_obj,
            {
                "kind": "device_followup",
                "role": "device",
                "payload": {"ok": True},
            },
        )
        from diagnostic_mas.case import set_issue_status

        for x in handoff.get("issue_ids") or []:
            set_issue_status(case_obj, x, "explained")

    with patch(
        "diagnostic_mas.coordinator.run_device_followup", new=fake_followup
    ):
        await run_autonomous_loop(
            object(),
            object(),  # type: ignore[arg-type]
            case,
            device_names=["renc-data-sw", "lbnl-data-sw"],
            llm_turn_fn=fake_turn,
        )

    assert case.budget.handoffs_used == 1
    assert case.handoffs
    assert case.hypotheses
    assert case.hypotheses[0]["text"] == "possible optic fault"
    text = render_report(case)
    facts = text.split("Hypothes")[0]
    assert "possible optic fault" not in facts
    assert "possible optic fault" in text


@pytest.mark.asyncio
async def test_llm_rejects_bad_handoff_falls_back_heuristic():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    _seed_underlay_issue(case)

    def fake_turn(_settings, **_kwargs):
        return {
            "plans": [],
            "handoffs": [
                {
                    "to_role": "device",
                    "issue_ids": ["missing-id"],
                    "reason": "bad",
                    "suggested_actions": ["rm_rf"],
                    "budget_cost": 1,
                    "devices": ["renc-data-sw"],
                }
            ],
            "hypotheses": [],
        }

    async def fake_followup(_client, case_obj, handoff):
        add_evidence(
            case_obj, {"kind": "device_followup", "role": "device", "payload": {}}
        )
        from diagnostic_mas.case import set_issue_status

        for x in handoff.get("issue_ids") or []:
            set_issue_status(case_obj, x, "explained")

    with patch(
        "diagnostic_mas.coordinator.run_device_followup", new=fake_followup
    ):
        await run_autonomous_loop(
            object(),
            object(),  # type: ignore[arg-type]
            case,
            device_names=["renc-data-sw", "lbnl-data-sw"],
            llm_turn_fn=fake_turn,
        )

    # Bad handoff rejected; heuristic handoff with get_hardware_health succeeds
    assert case.issues[0]["status"] == "explained"
    assert any(h.get("reason", "").startswith("heuristic") for h in case.handoffs)


@pytest.mark.asyncio
async def test_service_layer_llm_plans_use_service_role():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    eid = add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {},
        },
    )
    open_issue(
        case,
        code="service_degraded",
        message="l2ptp x: degraded",
        evidence_ids=[eid],
        layer="services",
        devices=["renc-data-sw"],
    )

    def fake_turn(_settings, **kwargs):
        assert kwargs.get("role") == "service"
        return {
            "plans": [
                {
                    "check": "get_interface_health",
                    "args": {"device_name": "renc-data-sw"},
                    "reason": "ac check",
                }
            ],
            "handoffs": [],
            "hypotheses": [],
        }

    async def fake_checks(_client, role, plan):
        assert role == "service"
        return [{"check": "get_interface_health", "ok": True}]

    with patch(
        "diagnostic_mas.coordinator.run_role_deep_checks", new=fake_checks
    ):
        await run_autonomous_loop(
            object(),
            object(),  # type: ignore[arg-type]
            case,
            device_names=["renc-data-sw"],
            llm_turn_fn=fake_turn,
        )

    assert case.budget.deep_checks_used == 1
    assert case.issues[0]["status"] == "explained"


@pytest.mark.asyncio
async def test_summary_llm_used_when_not_skip():
    case = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    add_evidence(case, {"kind": "spine", "layer": "underlay", "payload": {}})

    class S:
        fabric_api_key = "k"
        fabric_model = "m"

    def fake_summary(_settings, _case):
        return "Headline: lab underlay looks fine from Evidence."

    text = await summary_narrative(
        case, S(), skip_llm=False, llm_summary_fn=fake_summary  # type: ignore[arg-type]
    )
    assert "Headline" in text
    template = await summary_narrative(case, S(), skip_llm=True)  # type: ignore[arg-type]
    assert "LLM diagnosis skipped" in template
