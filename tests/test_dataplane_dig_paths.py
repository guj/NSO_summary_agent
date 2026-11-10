"""The service investigation loop's less common paths, driven by scripted LLM replies.

Each test feeds ``llm_dataplane_verify_one`` a short script of chat replies (or
errors) and fake MCP results, then checks what it asked the LLM, logged and
recorded. Nothing here contacts NSO or an LLM endpoint.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from diagnostic_mas import dataplane_verify as dv
from diagnostic_mas.case import Budget, CaseFile, DrillSession

CONCLUDE_ONLY = {"type": "function", "function": {"name": "conclude_dataplane"}}
BUDGET_ERROR = ("Error code: 429 - {'error': {'message': 'ExceededBudget: over budget', "
                "'type': 'budget_exceeded', 'code': '429'}}")


def _call(name, arguments, call_id="c1"):
    return NS(id=call_id, function=NS(
        name=name, arguments=arguments if isinstance(arguments, str) else json.dumps(arguments)))


def _reply(*calls, content=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=list(calls)), finish_reason="stop")])


def _conclusion(status="unknown"):
    return {"dataplane_status": status, "observed": "AC up on amst-data-sw",
            "cause": "PE-side checks only; customer delivery not tested"}


def _concludes(status="unknown", call_id="end"):
    return _reply(_call("conclude_dataplane", _conclusion(status), call_id))


def _mcp(tool_name, params, call_id="m1"):
    return _call("mcp_call", {"tool_name": tool_name, "params": params, "reason": "check"}, call_id)


class ScriptedLLM:
    """Answers each chat request with the next step of the script; an exception step is raised."""

    def __init__(self, *script):
        self.chat = self.completions = self
        self.script = list(script)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def tool_names(self, n):
        return [tool["function"]["name"] for tool in self.requests[n]["tools"]]

    def last_user_text(self, n):
        return self.user_texts(n)[-1]

    def user_texts(self, n):
        return [m["content"] for m in self.requests[n]["messages"] if m["role"] == "user"]


def _record(**extra):
    return {"name": "svc-a", "service_type": "l3rt", "system_status": "up",
            "dataplane_status": "not_checked", "devices": ["amst-data-sw"], **extra}


@pytest.fixture
def mcp(monkeypatch):
    """Fake MCP execution: each call spends one tool and returns the next scripted output."""
    calls = []

    def install(*outputs):
        script = list(outputs)

        async def execute(client, case, **kwargs):
            kwargs["session"].tools_used += 1
            calls.append(kwargs)
            return script.pop(0) if script else "interface up"

        monkeypatch.setattr(dv, "execute_one_drill_call", execute)
        return calls

    install()
    return install


async def _dig(llm, *, record=None, max_tools=5, used=0):
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    session = DrillSession(max_tools=max_tools)
    session.tools_used = used
    ok = await dv.llm_dataplane_verify_one(
        object(), NS(fabric_model="m"), case, record=record or _record(),
        device_names={"amst-data-sw", "dall-data-sw"}, session=session, openai_client=llm)
    return ok, case, session


def _payload(case, kind):
    return next(ev["payload"] for ev in case.evidence if ev.get("kind") == kind)


@pytest.mark.asyncio
async def test_a_service_with_no_tools_left_is_not_investigated(mcp):
    llm = ScriptedLLM()

    ok, case, _session = await _dig(llm, max_tools=2, used=2)

    assert ok is False
    assert llm.requests == [] and case.diagnoses == []


@pytest.mark.asyncio
async def test_the_first_request_names_unavailable_endpoints_and_the_fixed_check(mcp, monkeypatch):
    import nso_facts.mcp_client as mcp_client

    monkeypatch.setattr(mcp_client, "quarantined_devices", lambda: {"dall-data-sw": "timeout"})
    llm = ScriptedLLM(_concludes())

    await _dig(llm, record=_record(devices=["amst-data-sw", "dall-data-sw"],
                                   basic_checks={"status": "down"}))

    first = llm.requests[0]["messages"][1]["content"]
    assert "Do not call MCP on unavailable_devices; prefer available_devices." in first
    brief = json.loads(first[first.index("{"):])
    assert brief["unavailable_devices"] == ["dall-data-sw"]
    assert brief["available_devices"] == ["amst-data-sw"]
    assert brief["basic_checks"] == {"status": "down"}


@pytest.mark.asyncio
async def test_an_unreadable_quarantine_list_leaves_the_endpoints_out_of_the_request(mcp, monkeypatch):
    import nso_facts.mcp_client as mcp_client

    def broken():
        raise RuntimeError("no quarantine state")

    monkeypatch.setattr(mcp_client, "quarantined_devices", broken)
    llm = ScriptedLLM(_concludes())

    ok, _case, _session = await _dig(llm)

    assert ok is True
    assert "unavailable_devices" not in llm.requests[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_without_a_client_one_is_made_with_the_chat_timeout(mcp, monkeypatch):
    import agent.summarize as summarize

    llm = ScriptedLLM(_concludes())
    made = []
    monkeypatch.setattr(summarize, "fabric_openai_client",
                        lambda settings, timeout: made.append(timeout) or llm)
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))

    ok = await dv.llm_dataplane_verify_one(
        object(), NS(fabric_model="m"), case, record=_record(), device_names={"amst-data-sw"},
        session=DrillSession(max_tools=5))

    assert ok is True
    assert made == [dv.llm_timeout_seconds(NS(fabric_model="m"))]


@pytest.mark.asyncio
async def test_a_halted_run_records_the_service_unknown_without_asking_the_llm(mcp, monkeypatch):
    import diagnostic_mas.dataplane_scheduler as scheduler

    monkeypatch.setattr(scheduler, "parent_halted", lambda: True)
    llm = ScriptedLLM()

    ok, case, _session = await _dig(llm)

    assert ok is True and llm.requests == []
    incomplete = _payload(case, "dataplane_incomplete")
    assert incomplete["dataplane_status"] == "unknown"


@pytest.mark.asyncio
async def test_a_conclusion_written_as_text_is_recorded(mcp):
    llm = ScriptedLLM(_reply(content=json.dumps(_conclusion("up"))))

    ok, case, _session = await _dig(llm)

    assert ok is True and len(llm.requests) == 1
    assert case.diagnoses[-1]["status"] == "up"
    assert _payload(case, "dataplane_finding")["investigation"]["stop_reason"] == "concluded"


@pytest.mark.asyncio
async def test_a_rejected_forced_conclusion_request_is_retried_without_forcing(mcp, capsys):
    llm = ScriptedLLM(_reply(content="Still thinking."), RuntimeError("tool_choice not supported"),
                      _concludes())

    ok, case, _session = await _dig(llm)

    assert ok is True
    assert llm.requests[1]["tool_choice"] == CONCLUDE_ONLY
    assert llm.tool_names(1) == ["conclude_dataplane"]
    assert llm.requests[2]["tool_choice"] == "auto"
    assert case.diagnoses[-1]["status"] == "unknown"
    assert "forced tool_choice rejected (RuntimeError); retrying with tool_choice=auto" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_failed_retry_ends_the_service_unknown(mcp, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATAPLANE_LLM_DUMP", str(tmp_path))
    llm = ScriptedLLM(_reply(content="Still thinking."), RuntimeError("tool_choice not supported"),
                      ValueError("bad gateway"))

    ok, case, _session = await _dig(llm)

    assert ok is True and len(llm.requests) == 3
    assert "the LLM request failed (ValueError)" in _payload(case, "dataplane_incomplete")["cause"]
    err = capsys.readouterr().err
    assert "LLM error after" in err and "bad gateway" in err
    assert "dumped failed LLM request to" in err
    assert list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_a_provider_budget_error_on_the_retry_halts_the_run(mcp):
    from agent.llm_budget import ProviderBudgetExceeded

    llm = ScriptedLLM(_reply(content="Still thinking."), RuntimeError("tool_choice not supported"),
                      RuntimeError(BUDGET_ERROR))
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))

    with pytest.raises(ProviderBudgetExceeded):
        await dv.llm_dataplane_verify_one(
            object(), NS(fabric_model="m"), case, record=_record(), device_names={"amst-data-sw"},
            session=DrillSession(max_tools=5), openai_client=llm)

    assert case.llm_halt_reason
    assert _payload(case, "dataplane_incomplete")["dataplane_status"] == "unknown"


@pytest.mark.asyncio
async def test_a_provider_budget_error_saves_the_request_when_asked(mcp, monkeypatch, tmp_path, capsys):
    from agent.llm_budget import ProviderBudgetExceeded

    monkeypatch.setenv("DATAPLANE_LLM_DUMP", str(tmp_path))
    llm = ScriptedLLM(RuntimeError(BUDGET_ERROR))

    with pytest.raises(ProviderBudgetExceeded):
        await _dig(llm)

    assert "dumped failed LLM request to" in capsys.readouterr().err
    assert list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_a_conclusion_whose_arguments_are_not_json_is_refused(mcp, capsys):
    llm = ScriptedLLM(_reply(_call("conclude_dataplane", "{not json")), _concludes())

    ok, case, _session = await _dig(llm)

    assert ok is True and case.diagnoses[-1]["status"] == "unknown"
    refused = next(m for m in llm.requests[1]["messages"] if m["role"] == "tool")
    assert refused["content"] == (
        "ERROR: conclude_dataplane refused: arguments are not valid JSON. Send it again with "
        "dataplane_status, observed, cause as non-empty text. If the readings do not establish a "
        "cause, say that in cause.")
    assert "rejected conclude_dataplane" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_three_refused_conclusions_in_a_row_end_the_dig(mcp, capsys):
    # As the salt-seat drill on 9 Nov 2026, which tried 13 times until its round limit.
    no_cause = {"dataplane_status": "unknown", "observed": "AC up on amst-data-sw"}
    llm = ScriptedLLM(*(_reply(_call("conclude_dataplane", no_cause, f"c{n}")) for n in range(4)), _concludes())

    ok, case, _session = await _dig(llm, max_tools=10)

    assert ok is True and len(llm.requests) == 3
    told = next(m for m in llm.requests[1]["messages"] if m["role"] == "tool")["content"]
    assert told.startswith("ERROR: conclude_dataplane refused: no usable cause (missing; sent: dataplane_status, observed).")
    assert _payload(case, "dataplane_incomplete")["dataplane_status"] == "unknown"
    assert ("the model's conclusion was refused 3 times in a row: no usable cause"
            in json.dumps(_payload(case, "dataplane_incomplete")))
    assert "stop: the model's conclusion was refused 3 times in a row" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_read_between_refused_conclusions_starts_the_count_again(mcp):
    no_cause = {"dataplane_status": "unknown", "observed": "AC up on amst-data-sw"}
    refused = lambda n: _reply(_call("conclude_dataplane", no_cause, f"c{n}"))
    llm = ScriptedLLM(refused(1), refused(2), _reply(_mcp("exec_show", {"device_name": "amst-data-sw"})),
                      refused(3), refused(4), _concludes())

    ok, case, _session = await _dig(llm, max_tools=10)

    assert len(llm.requests) == 6 and case.diagnoses[-1]["status"] == "unknown"


@pytest.mark.asyncio
async def test_an_unknown_function_is_refused_by_name(mcp, capsys):
    llm = ScriptedLLM(_reply(_call("get_services", {})), _concludes())

    await _dig(llm)

    refused = next(m for m in llm.requests[1]["messages"] if m["role"] == "tool")
    assert refused["content"] == "ERROR: unknown function 'get_services'; use mcp_call or conclude_dataplane"
    assert "rejected get_services: unknown function" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_missing_tool_name_is_inferred_from_cli_params_or_refused(mcp, capsys):
    calls = mcp("Interface is up")
    llm = ScriptedLLM(
        _reply(_mcp("", {"device_name": "amst-data-sw", "command": "show interfaces brief"}, "m1"),
               _mcp("", {"color": "blue"}, "m2")),
        _concludes())

    _ok, _case, session = await _dig(llm)

    assert [call["tool_name"] for call in calls] == ["exec_show"]
    assert session.tools_used == 1
    tool_messages = [m for m in llm.requests[1]["messages"] if m["role"] == "tool"]
    assert tool_messages[1]["content"].startswith("ERROR: mcp_call requires non-empty tool_name")
    assert "mcp_call coerce: inferred tool_name=exec_show from CLI params" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_an_explore_path_without_a_module_is_refused_without_spending_a_tool(mcp):
    calls = mcp()
    llm = ScriptedLLM(_reply(_mcp("explore_nso_path", {"path": "services/l3rt"})), _concludes())

    _ok, _case, session = await _dig(llm)

    assert calls == [] and session.tools_used == 0
    refused = next(m for m in llm.requests[1]["messages"] if m["role"] == "tool")
    assert refused["content"].startswith("ERROR: Rejected")


@pytest.mark.asyncio
async def test_a_failed_tool_call_is_marked_as_no_health_evidence(mcp):
    mcp("ERROR: device timed out")
    llm = ScriptedLLM(_reply(_mcp("get_interface_health", {"device_name": "amst-data-sw"})), _concludes())

    _ok, case, _session = await _dig(llm)

    answer = next(m for m in llm.requests[1]["messages"] if m["role"] == "tool")
    assert answer["content"].startswith("ERROR: tool execution/CLI failed; not service-health evidence.")
    assert case.diagnoses[-1]["investigation"]["query_errors"] == 1


@pytest.mark.asyncio
async def test_spending_the_last_tool_leaves_a_round_to_conclude(mcp):
    llm = ScriptedLLM(_reply(_mcp("get_interface_health", {"device_name": "amst-data-sw"})), _concludes("up"))

    ok, case, _session = await _dig(llm, max_tools=1)

    assert ok is True and case.diagnoses[-1]["status"] == "up"
    assert "Tool budget is exhausted" in llm.last_user_text(1)


@pytest.mark.asyncio
async def test_a_dig_that_spends_its_tools_without_concluding_stops_at_the_tool_limit(mcp):
    health = _mcp("get_interface_health", {"device_name": "amst-data-sw"})
    llm = ScriptedLLM(_reply(health), _reply(health), _reply(health))

    ok, case, _session = await _dig(llm, max_tools=1)

    assert ok is True and len(llm.requests) == 3
    assert any("Budget nearly exhausted" in text for text in llm.user_texts(2))
    assert _payload(case, "dataplane_incomplete")["investigation"]["stop_reason"] == "tool_limit"


@pytest.mark.asyncio
async def test_a_dig_whose_calls_are_all_refused_stops_at_the_round_limit(mcp):
    llm = ScriptedLLM(*[_reply(_call("get_services", {}, f"x{n}")) for n in range(3)])

    ok, case, session = await _dig(llm, max_tools=1)

    assert ok is True and session.tools_used == 0 and len(llm.requests) == 3
    assert _payload(case, "dataplane_incomplete")["investigation"]["stop_reason"] == "round_limit"


@pytest.mark.asyncio
async def test_after_two_batches_without_new_output_only_a_conclusion_is_accepted(mcp):
    calls = mcp("ERROR: no answer", "ERROR: no answer")
    health = _mcp("get_interface_health", {"device_name": "amst-data-sw"})
    llm = ScriptedLLM(_reply(health), _reply(health), _reply(health))

    ok, case, _session = await _dig(llm)

    assert ok is True and len(calls) == 2
    assert "Two consecutive batches produced only errors" in llm.last_user_text(2)
    assert llm.tool_names(2) == ["conclude_dataplane"]
    incomplete = _payload(case, "dataplane_incomplete")
    assert incomplete["dataplane_status"] == "unknown"
    assert "did not call conclude_dataplane after a no-progress stop" in incomplete["cause"]


@pytest.mark.asyncio
async def test_a_failed_request_ends_the_service_unknown_and_is_saved_when_asked(mcp, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATAPLANE_LLM_DUMP", str(tmp_path))
    llm = ScriptedLLM(ValueError("bad gateway"))

    ok, case, _session = await _dig(llm)

    assert ok is True and len(llm.requests) == 1
    assert "the LLM request failed (ValueError)" in _payload(case, "dataplane_incomplete")["cause"]
    assert "dumped failed LLM request to" in capsys.readouterr().err
    assert list(tmp_path.iterdir())
