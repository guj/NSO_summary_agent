"""Device follow-up must not treat error-only probes as success."""

from __future__ import annotations

import pytest

from diagnostic_mas.case import Budget, CaseFile, open_issue
from diagnostic_mas.roles.device import run_device_followup


@pytest.mark.asyncio
async def test_device_followup_error_only_marks_needs_human(monkeypatch):
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    iid = open_issue(
        case,
        code="adjacency_down",
        message="down",
        evidence_ids=[],
        layer="underlay",
        devices=["renc-data-sw"],
    )

    async def fake_checks(_client, _role, plan):
        return [
            {
                "check": t["check"],
                "args": t["args"],
                "error": "device unreachable",
            }
            for t in plan
        ]

    monkeypatch.setattr(
        "diagnostic_mas.roles.device.run_role_deep_checks", fake_checks
    )
    await run_device_followup(
        object(),
        case,
        {
            "id": "ho_1",
            "devices": ["renc-data-sw"],
            "suggested_actions": ["get_hardware_health"],
            "issue_ids": [iid],
            "reason": "box check",
        },
    )
    assert case.issues[0]["status"] == "needs_human"
    follow = [e for e in case.evidence if e.get("kind") == "device_followup"]
    assert len(follow) == 1
    assert follow[0]["payload"]["results"][0].get("error")


@pytest.mark.asyncio
async def test_device_followup_success_marks_explained(monkeypatch):
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    iid = open_issue(
        case,
        code="adjacency_down",
        message="down",
        evidence_ids=[],
        layer="underlay",
        devices=["renc-data-sw"],
    )

    async def fake_checks(_client, _role, plan):
        return [
            {
                "check": t["check"],
                "args": t["args"],
                "result": {"ok": True},
            }
            for t in plan
        ]

    monkeypatch.setattr(
        "diagnostic_mas.roles.device.run_role_deep_checks", fake_checks
    )
    await run_device_followup(
        object(),
        case,
        {
            "id": "ho_1",
            "devices": ["renc-data-sw"],
            "suggested_actions": ["get_hardware_health"],
            "issue_ids": [iid],
        },
    )
    assert case.issues[0]["status"] == "explained"
