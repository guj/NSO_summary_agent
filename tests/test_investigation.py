"""Offline tests for generic timing and verification-gap reporting."""
from types import SimpleNamespace
import pytest
from diagnostic_mas.case import Budget, CaseFile, DrillSession
from diagnostic_mas.investigation import Investigation, normalize_gap
from diagnostic_mas.dataplane_verify import parse_dataplane_conclusion
from diagnostic_mas.state_paths import case_to_dict
from diagnostic_mas.operator_report import _incomplete_result_line, _incomplete_dig_next_action, format_run_details


def test_structured_gap_validation_and_no_traffic_inference():
    gap = {"blocker": "query_error", "missing_check": "reverse MAC installation",
           "next_check": "Use the exact BG:BD on PE-B"}
    finding = parse_dataplane_conclusion({
        "dataplane_status": "unknown", "observed": "MAC unavailable",
        "cause": "query rejected", "verification_gap": gap})
    assert finding["verification_gap"]["source"] == "llm"
    dx = {**finding, "complete": False}
    assert "query error" in _incomplete_result_line(dx)
    assert "customer" not in _incomplete_result_line(dx)
    assert _incomplete_dig_next_action(dx) == gap["next_check"]
    assert normalize_gap({"blocker": "invented"}) is None


@pytest.mark.asyncio
async def test_timing_accounts_failed_requests_and_mcp(monkeypatch):
    from diagnostic_mas import investigation as module
    now = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    meter = Investigation()
    def timeout(**kwargs):
        now[0] += 5
        raise TimeoutError()
    with pytest.raises(TimeoutError):
        meter.llm(timeout)
    assert meter.stop_reason == "llm_timeout"
    async def tool():
        now[0] += 2
        return "ok"
    assert await meter.mcp(tool) == "ok"
    now[0] += 1
    result = meter.finish(1, 40)
    assert result["duration_seconds"] == 8
    assert result["llm_seconds"] == 5
    assert result["mcp_seconds"] == 2
    assert result["other_seconds"] == 1
    assert result["tools_remaining"] == 39


@pytest.mark.asyncio
async def test_timeout_loop_persists_telemetry_and_gap(monkeypatch):
    from diagnostic_mas import dataplane_verify as dv
    async def catalog(*args):
        return "read-only tools"
    monkeypatch.setattr(dv, "tool_catalog", catalog)
    class OAI:
        def __init__(self):
            self.chat = self
            self.completions = self
        def create(self, **kwargs):
            raise TimeoutError("Request timed out")
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    record = {"name": "service", "service_type": "l2sts", "devices": ["pe"],
              "system_status": "up", "dataplane_status": "not_checked"}
    settings = SimpleNamespace(fabric_model="fake")
    await dv.llm_dataplane_verify_one(
        object(), settings, case, record=record, device_names={"pe"},
        session=DrillSession(max_tools=40), openai_client=OAI())
    dx = case_to_dict(case)["diagnoses"][-1]
    assert dx["investigation"]["stop_reason"] == "llm_timeout"
    assert dx["investigation"]["llm_requests"] == 1
    assert dx["investigation"]["tools_remaining"] == 40
    assert dx["verification_gap"]["blocker"] == "llm_timeout"
    assert dx["verification_gap"]["source"] == "runner"
    assert case.evidence[-1]["payload"]["investigation"] == dx["investigation"]
    text = "\n".join(format_run_details(case))
    assert "tools remaining: 40" in text
    assert "llm_timeout" in text


@pytest.mark.asyncio
async def test_model_unknown_retains_specific_gap(monkeypatch):
    from diagnostic_mas import dataplane_verify as dv
    async def catalog(*args):
        return "read-only tools"
    monkeypatch.setattr(dv, "tool_catalog", catalog)
    import json
    finding = {"dataplane_status": "unknown", "observed": "MAC query failed",
               "cause": "Reverse installation unverified",
               "verification_gap": {"blocker": "query_error",
                   "missing_check": "reverse install", "direction": "B→A",
                   "next_check": "Correct the object name on PE-A"}}
    class OAI:
        def __init__(self):
            self.chat = self
            self.completions = self
        def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(finding), tool_calls=[]),
                finish_reason="stop")])
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    record = {"name": "service", "service_type": "l2sts", "devices": ["pe"],
              "system_status": "up", "dataplane_status": "not_checked"}
    await dv.llm_dataplane_verify_one(
        object(), SimpleNamespace(fabric_model="fake"), case, record=record,
        device_names={"pe"}, session=DrillSession(max_tools=40), openai_client=OAI())
    dx = case.diagnoses[-1]
    assert dx["verification_gap"]["direction"] == "B→A"
    assert dx["investigation"]["stop_reason"] == "concluded_insufficient_evidence"
    assert dx["investigation"]["rounds_used"] == 1


@pytest.mark.asyncio
async def test_round_limit_distinct_from_tool_budget(monkeypatch):
    from diagnostic_mas import dataplane_verify as dv
    monkeypatch.setattr(dv, "_DATAPLANE_MAX_ROUNDS_CAP", 1)
    async def catalog(*args):
        return "tools"
    monkeypatch.setattr(dv, "tool_catalog", catalog)
    class OAI:
        def __init__(self):
            self.chat = self
            self.completions = self
        def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[]),
                finish_reason="stop")])
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    await dv.llm_dataplane_verify_one(
        object(), SimpleNamespace(fabric_model="fake"), case,
        record={"name": "s", "service_type": "l2sts", "devices": ["pe"],
                "system_status": "up"}, device_names={"pe"},
        session=DrillSession(max_tools=40), openai_client=OAI())
    dx = case.diagnoses[-1]
    assert dx["investigation"]["stop_reason"] == "round_limit"
    assert dx["investigation"]["tools_remaining"] == 40


def test_failed_llm_retry_is_included_in_timing(monkeypatch):
    from diagnostic_mas import investigation as module
    now = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    meter = Investigation()
    def rejected():
        now[0] += 3
        raise ValueError("tool choice unsupported")
    with pytest.raises(ValueError):
        meter.llm(rejected)
    def success():
        now[0] += 2
        return "ok"
    assert meter.llm(success) == "ok"
    result = meter.finish(0, 40)
    assert result["llm_seconds"] == 5
    assert result["llm_requests"] == 2
    assert result["stop_reason"] == "no_conclusion"
