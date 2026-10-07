"""Tests for what a service investigation is told about devices dropped earlier in the run."""

from __future__ import annotations

import json

import pytest

from diagnostic_mas.case import Budget, CaseFile
from diagnostic_mas.dataplane_verify import llm_dataplane_verify_one
from diagnostic_mas.drill import DrillSession

CONCLUSION = '{"dataplane_status":"down","observed":"AC down","cause":"no receive light"}'


class _Reply:
    """An LLM reply that concludes at once."""

    def __init__(self) -> None:
        call = type("Call", (), {})()
        call.id = "c1"
        call.function = type("Fn", (), {"name": "conclude_dataplane", "arguments": CONCLUSION, "id": "c1"})()
        message = type("Msg", (), {"tool_calls": [call], "content": None})()
        self.choices = [type("Choice", (), {"message": message})()]


class _Llm:
    """Records the first request, which carries the service brief."""

    def __init__(self) -> None:
        self.chat = self.completions = self
        self.first_request: list[dict] | None = None

    def create(self, **kwargs):
        if self.first_request is None:
            self.first_request = kwargs["messages"]
        return _Reply()


class _Settings:
    fabric_model = "m"
    fabric_api_key = "k"
    fabric_api_url = "http://llm.invalid"


async def _brief(monkeypatch, *, endpoints: list[str], dropped: list[str]) -> tuple[dict, str]:
    monkeypatch.setattr(
        "nso_facts.mcp_client.quarantined_devices", lambda: {d: "timed out" for d in dropped}
    )
    llm = _Llm()
    record = {"name": "svc1", "service_type": "l2bridge", "system_status": "up",
              "dataplane_status": "not_checked", "status": "up", "devices": endpoints}
    await llm_dataplane_verify_one(
        object(), _Settings(),
        CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_tools_per_drill=5)),
        record=record, device_names=set(endpoints),
        session=DrillSession(max_tools=5, issue_edge_id="svc1"), openai_client=llm,
    )
    text = next(m["content"] for m in llm.first_request if m["role"] == "user")
    return json.loads(text[text.index("{"):]), text


@pytest.mark.asyncio
async def test_a_device_dropped_elsewhere_is_not_presented_as_part_of_the_service(monkeypatch):
    brief, text = await _brief(monkeypatch, endpoints=["mich-data-sw"], dropped=["scm-data-sw"])

    assert "scm-data-sw" not in text
    assert "unavailable_devices" not in brief
    assert "unavailable_devices" not in text


@pytest.mark.asyncio
async def test_a_dropped_endpoint_of_the_service_is_listed_as_unavailable(monkeypatch):
    brief, _text = await _brief(
        monkeypatch,
        endpoints=["rutg-data-sw", "fiu-data-sw"],
        dropped=["fiu-data-sw", "scm-data-sw"],
    )

    assert brief["unavailable_devices"] == ["fiu-data-sw"]
    assert brief["available_devices"] == ["rutg-data-sw"]
