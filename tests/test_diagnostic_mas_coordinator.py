from unittest.mock import AsyncMock, patch

import pytest

from diagnostic_mas.case import Budget, CaseFile, add_evidence, open_issue
from diagnostic_mas.coordinator import run_autonomous_loop, run_mandatory_spines


@pytest.mark.asyncio
async def test_mandatory_spines_call_order_and_ingest():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    settings = object()
    client = object()
    physical = [{"id": "p1"}]

    isis = {
        "static_summary": {"n": 1},
        "operational_summary": {"up": 1},
        "issues": [],
        "static_edges": [],
        "operational_edges": [],
    }
    bgp = {
        "static_summary": {"n": 1},
        "operational_summary": {"up": 1},
        "issues": [],
        "static_edges": [],
        "operational_edges": [],
    }
    svc = {
        "static_summary": {"service_keys": 0},
        "operational_summary": {},
        "issues": [],
        "static_edges": [],
        "operational_edges": [],
        "extra": {},
    }

    order: list[str] = []

    async def fake_isis(*_a, **_k):
        order.append("isis")
        return isis

    async def fake_bgp(*_a, **_k):
        order.append("bgp")
        return bgp

    async def fake_svc(*_a, **_k):
        order.append("service")
        return svc

    with (
        patch(
            "diagnostic_mas.coordinator.collect_static_physical",
            new=AsyncMock(return_value=(physical, [], {})),
        ),
        patch("diagnostic_mas.coordinator.run_isis_spine", new=fake_isis),
        patch("diagnostic_mas.coordinator.run_bgp_spine", new=fake_bgp),
        patch("diagnostic_mas.coordinator.run_service_spine", new=fake_svc),
    ):
        edges = await run_mandatory_spines(
            client, settings, case, ["renc-data-sw"]  # type: ignore[arg-type]
        )

    assert edges == physical
    assert order == ["isis", "bgp", "service"]
    assert len(case.evidence) == 4  # physical + isis + bgp + service
    assert case.evidence[0]["role"] == "physical"


@pytest.mark.asyncio
async def test_autonomous_loop_service_falls_back_to_device_handoff():
    """Empty LLM turn for services → heuristic Device follow-up."""
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    eid = add_evidence(case, {"kind": "spine", "payload": {}})
    open_issue(
        case,
        code="service_degraded",
        message="l2ptp bad: degraded",
        evidence_ids=[eid],
        layer="services",
        devices=["renc-data-sw"],
    )
    client = object()
    settings = object()

    def empty_turn(_settings, **_kwargs):
        return {"plans": [], "handoffs": [], "hypotheses": []}

    async def fake_followup(_client, case_obj, handoff):
        add_evidence(
            case_obj,
            {
                "kind": "device_followup",
                "role": "device",
                "payload": {"handoff_id": handoff.get("id")},
            },
        )
        from diagnostic_mas.case import set_issue_status

        for iid in handoff.get("issue_ids") or []:
            set_issue_status(case_obj, iid, "explained")

    with patch(
        "diagnostic_mas.coordinator.run_device_followup", new=fake_followup
    ):
        await run_autonomous_loop(
            client,
            settings,  # type: ignore[arg-type]
            case,
            device_names=["renc-data-sw"],
            llm_turn_fn=empty_turn,
        )

    assert any(e.get("kind") == "device_followup" for e in case.evidence)
    assert case.handoffs
    assert case.issues[0]["status"] == "explained"
