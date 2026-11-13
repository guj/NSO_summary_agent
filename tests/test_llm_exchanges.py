"""Each LLM investigation's conversation is kept with the run, so a later prompt can be replayed against it."""
import json

import pytest

from diagnostic_mas.case import Budget, CaseFile, DrillSession
from diagnostic_mas.llm_exchanges import MAX_EXCHANGE_CHARS, record_exchange
from diagnostic_mas.state_paths import persist_case, prune_saved_runs


def _messages(reply="Hu0/0/0/1 is up"):
    return [
        {"role": "system", "content": "You are the dataplane agent. RULES"},
        {"role": "user", "content": "Check this service"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "mcp_call", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": reply},
    ]


def test_an_exchange_keeps_the_conversation_and_stores_the_system_prompt_once():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))

    record_exchange(case, kind="dataplane", subject={"name": "svc1"}, messages=_messages(), model="m",
                    outcome={"concluded": True})
    record_exchange(case, kind="dataplane", subject={"name": "svc2"}, messages=_messages("Hu0/0/0/1 is down"),
                    model="m", outcome={"concluded": False, "stop_reason": "budget"})

    first, second = case.llm_exchanges
    assert first["system"] == second["system"]                                # the same prompt, one copy
    assert list(case.llm_texts.values()) == ["You are the dataplane agent. RULES"]
    assert [m["role"] for m in first["messages"]] == ["user", "assistant", "tool"]   # the prompt is not repeated in the body
    assert first["messages"][-1]["content"] == "Hu0/0/0/1 is up"             # what the model was shown
    assert second["messages"][-1]["content"] == "Hu0/0/0/1 is down"
    assert (first["kind"], first["subject"], first["model"], first["outcome"]) == (
        "dataplane", {"name": "svc1"}, "m", {"concluded": True})
    assert first["recorded_at"]


def test_a_conversation_over_the_limit_is_cut_and_says_so():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    big = "x" * (MAX_EXCHANGE_CHARS + 5000)

    record_exchange(case, kind="drill", subject={}, messages=_messages(big), model="m", outcome={})

    [exchange] = case.llm_exchanges
    assert exchange["truncated"] is True
    assert len(json.dumps(exchange["messages"])) <= MAX_EXCHANGE_CHARS + 2000
    assert exchange["messages"][0]["content"] == "Check this service"        # the start of the conversation is kept


def test_nothing_is_recorded_without_messages():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    record_exchange(case, kind="drill", subject={}, messages=[], model="m", outcome={})
    assert case.llm_exchanges == [] and case.llm_texts == {}


def test_the_run_folder_gets_llm_exchanges_json_only_when_there_were_exchanges(tmp_path):
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    persist_case(tmp_path, run_id="20261112T000000Z", case=case)
    assert not (tmp_path / "runs" / "20261112T000000Z" / "llm_exchanges.json").exists()

    record_exchange(case, kind="port", subject={"port": "Hu0/0/0/64"}, messages=_messages(), model="m", outcome={})
    persist_case(tmp_path, run_id="20261112T010000Z", case=case)

    saved = json.loads((tmp_path / "runs" / "20261112T010000Z" / "llm_exchanges.json").read_text())
    assert saved["format_version"] == 1
    assert saved["exchanges"][0]["kind"] == "port"
    assert list(saved["texts"].values()) == ["You are the dataplane agent. RULES"]
    assert saved["exchanges"][0]["system"] in saved["texts"]
    # case.json stays small: the conversations are not in it.
    assert "RULES" not in (tmp_path / "runs" / "20261112T010000Z" / "case.json").read_text()


def test_an_old_runs_exchanges_are_pruned_with_its_other_files(tmp_path):
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    record_exchange(case, kind="port", subject={}, messages=_messages(), model="m", outcome={})
    persist_case(tmp_path, run_id="20261101T000000Z", case=case)
    old = tmp_path / "runs" / "20261101T000000Z"
    assert (old / "llm_exchanges.json").exists()
    persist_case(tmp_path, run_id="20261111T000000Z", case=case)          # latest.json now names a newer run

    prune_saved_runs(tmp_path, run_id="20261112T000000Z", keep_days=7)

    assert not old.exists()                      # every file went, so the folder went with them


class _Fn:
    def __init__(self, name, arguments, id_):
        self.name, self.arguments, self.id = name, arguments, id_


class _Call:
    def __init__(self, fn):
        self.function, self.id = fn, fn.id


class _Msg:
    def __init__(self, tool_calls=None, content=None):
        self.tool_calls, self.content = tool_calls, content


class _Resp:
    def __init__(self, message):
        self.choices = [type("C", (), {"message": message, "finish_reason": None})()]


@pytest.mark.asyncio
async def test_a_drill_investigation_is_kept_with_what_the_device_answered(monkeypatch):
    from diagnostic_mas.drill import llm_drill_tool_loop
    from diagnostic_mas.case import open_issue

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=2))
    open_issue(case, code="service_degraded", message="degraded", evidence_ids=[], layer="services",
               edge_id="x", devices=["renc-data-sw"])
    issue = case.issues[0]
    session = DrillSession(max_tools=2, issue_id=issue["id"])

    async def fake_deep(client, plan):
        return [{"check": "exec_show", "result": "Hu0/0/0/0.100 is up"}]
    monkeypatch.setattr("diagnostic_mas.drill.run_deep_checks", fake_deep)

    class FakeOAI:
        def __init__(self):
            self.chat = self.completions = self
            self.n = 0

        def create(self, **kwargs):
            self.n += 1
            if self.n == 1:
                args = json.dumps({"tool_name": "exec_show", "reason": "verify AC", "params": {
                    "device_name": "renc-data-sw", "input_command": "interfaces Hu0/0/0/0.100"}})
                return _Resp(_Msg(tool_calls=[_Call(_Fn("mcp_call", args, "c1"))]))
            return _Resp(_Msg(content="done"))

    class S:
        fabric_api_key = "k"
        fabric_model = "model-x"

    await llm_drill_tool_loop(object(), S(), case, device_names={"renc-data-sw"}, focus_issue=issue,
                              session=session, openai_client=FakeOAI(), account="port")

    [exchange] = case.llm_exchanges
    assert (exchange["kind"], exchange["model"]) == ("port", "model-x")
    assert exchange["subject"]["issue_id"] == issue["id"]
    assert exchange["outcome"]["concluded"] is False
    roles = [m["role"] for m in exchange["messages"]]
    assert roles[0] == "user" and "assistant" in roles and roles[-1] == "tool"
    assert any("Hu0/0/0/0.100 is up" in str(m.get("content")) for m in exchange["messages"] if m["role"] == "tool")
    assert case.llm_texts[exchange["system"]]                          # the prompt the model was given


@pytest.mark.asyncio
async def test_a_service_investigation_is_kept_with_its_conclusion(monkeypatch):
    import diagnostic_mas.dataplane_verify as dv

    class FakeOAI:
        def __init__(self):
            self.chat = self.completions = self
            self.n = 0

        def create(self, **kwargs):
            self.n += 1
            if self.n == 1:
                args = json.dumps({"tool_name": "get_interface_health", "params": {"device": "lbnl-data-sw"}})
                return _Resp(_Msg(tool_calls=[_Call(_Fn("mcp_call", args, "t1"))]))
            return _Resp(_Msg(tool_calls=[_Call(_Fn(
                "conclude_dataplane",
                '{"dataplane_status":"down","observed":"used last tool result","cause":"AC down from final probe"}',
                "c1"))]))

    async def fake_exec(client, case, **kwargs):
        return "interface down"
    monkeypatch.setattr(dv, "execute_one_drill_call", fake_exec)

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    rec = {"name": "svc1", "service_type": "l2ptp", "system_status": "up", "dataplane_status": "not_checked",
           "devices": ["lbnl-data-sw"]}

    class S:
        fabric_model = "model-x"
        fabric_api_key = "k"
        fabric_api_url = "http://x"

    await dv.llm_dataplane_verify_one(object(), S(), case, record=rec, device_names={"lbnl-data-sw"},
                                      session=DrillSession(max_tools=1), openai_client=FakeOAI())

    [exchange] = case.llm_exchanges
    assert (exchange["kind"], exchange["model"]) == ("dataplane", "model-x")
    assert exchange["subject"] == {"service_type": "l2ptp", "name": "svc1"}
    assert exchange["outcome"]["concluded"] is True
    assert exchange["outcome"]["detail"] is None                         # the loop's free-text reason is kept only when it did not conclude
    assert exchange["outcome"]["stop_reason"] == "concluded"            # the investigation's own reason, not a default text
    tool_replies = [m["content"] for m in exchange["messages"] if m["role"] == "tool"]
    assert "interface down" in tool_replies
    assert exchange["messages"][0]["role"] == "user" and "l2ptp" in exchange["messages"][0]["content"]
