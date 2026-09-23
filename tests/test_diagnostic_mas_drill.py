"""Tests for diagnostic_mas drill phase."""

from __future__ import annotations

import json

import pytest

from diagnostic_mas.case import Budget, CaseFile, add_evidence, open_issue
from diagnostic_mas.deep_checks import DRILL_ALLOWLIST


def test_drill_allowlist_has_expected_tools():
    from diagnostic_mas.deep_checks import DATAPLANE_ALLOWLIST, DRILL_ALLOWLIST

    assert "exec_show" in DRILL_ALLOWLIST
    assert "get_interface_health" in DRILL_ALLOWLIST
    assert "check_service_sync" in DRILL_ALLOWLIST
    assert "get_hardware_health" in DRILL_ALLOWLIST
    assert "verify_bgp_peer_reachability" not in DRILL_ALLOWLIST
    assert "get_device_config" in DATAPLANE_ALLOWLIST
    assert "explore_nso_path" in DATAPLANE_ALLOWLIST
    assert "compare_service_config" in DATAPLANE_ALLOWLIST
    assert "sync_from_device" not in DATAPLANE_ALLOWLIST


@pytest.mark.asyncio
async def test_execute_drill_plans_caps_at_max_and_writes_evidence(monkeypatch):
    from diagnostic_mas.case import DrillSession
    from diagnostic_mas.drill import execute_drill_plans

    case = CaseFile(
        budget=Budget(
            max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=2
        )
    )
    session = DrillSession(max_tools=2)
    plans = [
        {
            "check": "exec_show",
            "args": {
                "device_name": "renc-data-sw",
                "input_command": "interfaces Hu0/0/0/0.100",
            },
            "reason": "a",
        },
        {
            "check": "get_interface_health",
            "args": {"device_name": "renc-data-sw"},
            "reason": "b",
        },
        {
            "check": "exec_show",
            "args": {
                "device_name": "renc-data-sw",
                "input_command": "l2vpn xconnect",
            },
            "reason": "c",
        },
    ]

    async def fake_deep(client, plan):
        return [
            {"check": t["check"], "args": t.get("args"), "result": {"ok": True}}
            for t in plan
        ]

    monkeypatch.setattr("diagnostic_mas.drill.run_deep_checks", fake_deep)
    out = await execute_drill_plans(
        object(),
        case,
        plans,
        device_names={"renc-data-sw", "lbnl-data-sw"},
        session=session,
    )
    assert case.budget.drills_used == 2
    assert session.tools_used == 2
    assert len(out) == 2
    drills = [e for e in case.evidence if e.get("kind") == "drill"]
    assert len(drills) == 2


@pytest.mark.asyncio
async def test_execute_rejects_non_allowlisted(monkeypatch):
    from diagnostic_mas.drill import execute_drill_plans

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=2))
    called = False

    async def fake_deep(client, plan):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("diagnostic_mas.drill.run_deep_checks", fake_deep)
    await execute_drill_plans(
        object(),
        case,
        [
            {
                "check": "sync_from_device",
                "args": {"device_name": "renc-data-sw"},
                "reason": "nope",
            }
        ],
        device_names={"renc-data-sw"},
    )
    assert called is False
    assert case.budget.drills_used == 0


def test_compact_drill_context_includes_live_l2():
    from diagnostic_mas.drill import compact_drill_context

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=2))
    open_issue(
        case,
        code="service_degraded",
        message="l2ptp x: degraded — renc Hu0/0/0/0.100 AC DN",
        evidence_ids=[],
        layer="services",
        edge_id="x",
        devices=["renc-data-sw", "lbnl-data-sw"],
        live_l2={
            "summary": "degraded",
            "endpoints": [
                {"device": "lbnl-data-sw", "ac": "Hu0/0/0/17.100", "st": "UP"},
                {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "DN"},
            ],
        },
    )
    ctx = compact_drill_context(case)
    assert ctx["remaining_tool_calls"] == 2
    assert ctx["issues"][0]["live_l2"]["endpoints"][1]["st"] == "DN"


def test_parse_drill_plans_object_and_array():
    from diagnostic_mas.drill import parse_drill_plans

    assert (
        len(
            parse_drill_plans(
                '{"plans":[{"check":"exec_show","args":{"device_name":"a","input_command":"isis neighbors"},"reason":"x"}]}'
            )
        )
        == 1
    )
    assert (
        len(
            parse_drill_plans(
                '[{"check":"exec_show","args":{"device_name":"a","input_command":"isis neighbors"},"reason":"x"}]'
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_run_drill_phase_skip_when_max_zero(monkeypatch):
    from diagnostic_mas.drill import run_drill_phase

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_drill_issues=0, max_tools_per_drill=0))
    called = False

    async def boom(*a, **k):
        nonlocal called
        called = True
        return 0

    await run_drill_phase(
        object(),
        object(),
        case,
        device_names={"renc-data-sw"},
        skip_llm=False,
        tool_loop_fn=boom,
    )
    assert called is False


@pytest.mark.asyncio
async def test_run_drill_phase_uses_tool_loop(monkeypatch):
    from diagnostic_mas.drill import execute_one_drill_call, run_drill_phase

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=2))
    open_issue(
        case,
        code="service_degraded",
        message="degraded",
        evidence_ids=[],
        layer="services",
        edge_id="x",
        devices=["renc-data-sw"],
    )

    async def fake_deep(client, plan):
        return [{"check": "exec_show", "result": {"ok": True}}]

    monkeypatch.setattr("diagnostic_mas.drill.run_deep_checks", fake_deep)

    async def fake_loop(mcp_client, settings, case, *, device_names):
        from diagnostic_mas.case import DrillSession

        session = DrillSession(max_tools=case.budget.max_tools_per_drill)
        await execute_one_drill_call(
            mcp_client,
            case,
            tool_name="exec_show",
            params={
                "device_name": "renc-data-sw",
                "input_command": "interfaces Hu0/0/0/0.100",
            },
            reason="AC DN",
            device_names=device_names,
            session=session,
        )
        return 1

    class S:
        fabric_api_key = "k"
        fabric_model = "m"

    await run_drill_phase(
        object(),
        S(),
        case,
        device_names={"renc-data-sw"},
        skip_llm=False,
        tool_loop_fn=fake_loop,
    )
    assert case.budget.drills_used == 1
    assert any(e.get("kind") == "drill" for e in case.evidence)


@pytest.mark.asyncio
async def test_llm_drill_tool_loop_calls_mcp(monkeypatch):
    from diagnostic_mas.case import DrillSession
    from diagnostic_mas.drill import llm_drill_tool_loop

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=2))
    open_issue(
        case,
        code="service_degraded",
        message="degraded",
        evidence_ids=[],
        layer="services",
        edge_id="x",
        devices=["renc-data-sw"],
        live_l2={
            "summary": "degraded",
            "endpoints": [
                {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "DN"},
            ],
        },
    )
    issue = case.issues[0]
    session = DrillSession(max_tools=2, issue_id=issue["id"])

    async def fake_deep(client, plan):
        return [{"check": "exec_show", "result": "Hu0/0/0/0.100 is up"}]

    monkeypatch.setattr("diagnostic_mas.drill.run_deep_checks", fake_deep)

    class _Fn:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class _Call:
        def __init__(self, cid, name, arguments):
            self.id = cid
            self.function = _Fn(name, arguments)

    class _Msg:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, message):
            self.message = message

    class _Resp:
        def __init__(self, message):
            self.choices = [_Choice(message)]

    round_n = {"n": 0}

    class FakeOAI:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    round_n["n"] += 1
                    if round_n["n"] == 1:
                        return _Resp(
                            _Msg(
                                tool_calls=[
                                    _Call(
                                        "c1",
                                        "mcp_call",
                                        json.dumps(
                                            {
                                                "tool_name": "exec_show",
                                                "params": {
                                                    "device_name": "renc-data-sw",
                                                    "input_command": (
                                                        "interfaces Hu0/0/0/0.100"
                                                    ),
                                                },
                                                "reason": "verify AC",
                                            }
                                        ),
                                    )
                                ]
                            )
                        )
                    return _Resp(_Msg(content="done"))

    class S:
        fabric_api_key = "k"
        fabric_model = "m"

    tools, concluded = await llm_drill_tool_loop(
        object(),
        S(),
        case,
        device_names={"renc-data-sw"},
        focus_issue=issue,
        session=session,
        openai_client=FakeOAI(),
    )
    assert tools == 1
    assert concluded is False
    assert case.budget.drills_used == 1
    drills = [e for e in case.evidence if e.get("kind") == "drill"]
    assert len(drills) == 1
    assert drills[0]["payload"]["check"] == "exec_show"


@pytest.mark.asyncio
async def test_llm_drill_concludes_after_batch(monkeypatch):
    from diagnostic_mas.case import DrillSession
    from diagnostic_mas.drill import llm_drill_tool_loop

    case = CaseFile(
        budget=Budget(
            max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=5
        )
    )
    open_issue(
        case,
        code="service_degraded",
        message="degraded",
        evidence_ids=[],
        layer="services",
        edge_id="x",
        devices=["renc-data-sw", "lbnl-data-sw"],
    )
    issue = case.issues[0]
    session = DrillSession(max_tools=5, issue_id=issue["id"])

    async def fake_deep(client, plan):
        return [{"check": "exec_show", "result": "ok"}]

    monkeypatch.setattr("diagnostic_mas.drill.run_deep_checks", fake_deep)

    class _Fn:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class _Call:
        def __init__(self, cid, name, arguments):
            self.id = cid
            self.function = _Fn(name, arguments)

    class _Msg:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, message):
            self.message = message

    class _Resp:
        def __init__(self, message):
            self.choices = [_Choice(message)]

    round_n = {"n": 0}

    class FakeOAI:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    round_n["n"] += 1
                    if round_n["n"] == 1:
                        # Batch: both ends in one turn
                        return _Resp(
                            _Msg(
                                tool_calls=[
                                    _Call(
                                        "c1",
                                        "mcp_call",
                                        json.dumps(
                                            {
                                                "tool_name": "exec_show",
                                                "params": {
                                                    "device_name": "renc-data-sw",
                                                    "input_command": "l2vpn xconnect",
                                                },
                                            }
                                        ),
                                    ),
                                    _Call(
                                        "c2",
                                        "mcp_call",
                                        json.dumps(
                                            {
                                                "tool_name": "exec_show",
                                                "params": {
                                                    "device_name": "lbnl-data-sw",
                                                    "input_command": "l2vpn xconnect",
                                                },
                                            }
                                        ),
                                    ),
                                ]
                            )
                        )
                    return _Resp(
                        _Msg(
                            tool_calls=[
                                _Call(
                                    "c3",
                                    "conclude_investigation",
                                    json.dumps(
                                        {
                                            "observed": "l2ptp x degraded; AC UP XC DN",
                                            "cause": "EVPN local-id mismatch 99 vs 100",
                                            "fix_suggestion": (
                                                "align EVPN/AC id on renc to 100 "
                                                "(human must approve)"
                                            ),
                                            "confidence": "high",
                                        }
                                    ),
                                )
                            ]
                        )
                    )

    class S:
        fabric_api_key = "k"
        fabric_model = "m"

    tools, concluded = await llm_drill_tool_loop(
        object(),
        S(),
        case,
        device_names={"renc-data-sw", "lbnl-data-sw"},
        focus_issue=issue,
        session=session,
        openai_client=FakeOAI(),
    )
    assert tools == 2
    assert concluded is True
    assert round_n["n"] == 2
    findings = [e for e in case.evidence if e.get("kind") == "drill_finding"]
    assert len(findings) == 1
    assert "99 vs 100" in findings[0]["payload"]["cause"]


@pytest.mark.asyncio
async def test_llm_drill_final_turn_after_last_tool(monkeypatch):
    """Last mcp_call may exhaust tools; still allow one conclude_investigation round."""
    from diagnostic_mas.case import DrillSession
    from diagnostic_mas.drill import llm_drill_tool_loop

    case = CaseFile(
        budget=Budget(
            max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=1
        )
    )
    open_issue(
        case,
        code="service_degraded",
        message="degraded",
        evidence_ids=[],
        layer="services",
        edge_id="x",
        devices=["renc-data-sw"],
    )
    issue = case.issues[0]
    session = DrillSession(max_tools=1, issue_id=issue["id"])

    async def fake_deep(client, plan):
        return [{"check": "exec_show", "result": "XC DN AC UP"}]

    monkeypatch.setattr("diagnostic_mas.drill.run_deep_checks", fake_deep)

    class _Fn:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class _Call:
        def __init__(self, cid, name, arguments):
            self.id = cid
            self.function = _Fn(name, arguments)

    class _Msg:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, message):
            self.message = message

    class _Resp:
        def __init__(self, message):
            self.choices = [_Choice(message)]

    round_n = {"n": 0}
    saw_conclude_nudge = {"v": False}

    class FakeOAI:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    round_n["n"] += 1
                    msgs = kwargs.get("messages") or []
                    if any(
                        isinstance(m, dict)
                        and m.get("role") == "user"
                        and "budget is exhausted" in str(m.get("content") or "").lower()
                        for m in msgs
                    ):
                        saw_conclude_nudge["v"] = True
                    if round_n["n"] == 1:
                        return _Resp(
                            _Msg(
                                tool_calls=[
                                    _Call(
                                        "c1",
                                        "mcp_call",
                                        json.dumps(
                                            {
                                                "tool_name": "exec_show",
                                                "params": {
                                                    "device_name": "renc-data-sw",
                                                    "input_command": "l2vpn xconnect",
                                                },
                                            }
                                        ),
                                    )
                                ]
                            )
                        )
                    return _Resp(
                        _Msg(
                            tool_calls=[
                                _Call(
                                    "c2",
                                    "conclude_investigation",
                                    json.dumps(
                                        {
                                            "observed": "XC DN after last show",
                                            "cause": "remote unset",
                                            "confidence": "medium",
                                        }
                                    ),
                                )
                            ]
                        )
                    )

    class S:
        fabric_api_key = "k"
        fabric_model = "m"

    tools, concluded = await llm_drill_tool_loop(
        object(),
        S(),
        case,
        device_names={"renc-data-sw"},
        focus_issue=issue,
        session=session,
        openai_client=FakeOAI(),
    )
    assert tools == 1
    assert session.tools_used == 1
    assert concluded is True
    assert round_n["n"] == 2
    assert saw_conclude_nudge["v"] is True
    findings = [e for e in case.evidence if e.get("kind") == "drill_finding"]
    assert len(findings) == 1
    assert "remote unset" in findings[0]["payload"]["cause"]


def test_parse_drill_finding():
    from diagnostic_mas.drill import parse_drill_finding

    f = parse_drill_finding(
        {"observed": "svc degraded", "cause": "ac-id mismatch", "fix_suggestion": "x"}
    )
    assert f and f["cause"] == "ac-id mismatch"
    assert parse_drill_finding({"observed": "only"}) is None


@pytest.mark.asyncio
async def test_execute_one_drill_rejects_non_allowlisted(monkeypatch):
    from diagnostic_mas.drill import execute_one_drill_call

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=2))
    called = False

    async def fake_deep(client, plan):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("diagnostic_mas.drill.run_deep_checks", fake_deep)
    msg = await execute_one_drill_call(
        object(),
        case,
        tool_name="sync_from_device",
        params={"device_name": "renc-data-sw"},
        reason="nope",
        device_names={"renc-data-sw"},
    )
    assert "rejected" in msg.lower() or "error" in msg.lower()
    assert called is False
    assert case.budget.drills_used == 0


@pytest.mark.asyncio
async def test_execute_one_drill_rejects_ping_and_bad_sync_without_debit(monkeypatch):
    from diagnostic_mas.drill import execute_one_drill_call

    case = CaseFile(
        budget=Budget(
            max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=4
        )
    )
    called = False

    async def fake_deep(client, plan):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("diagnostic_mas.drill.run_deep_checks", fake_deep)
    ping_msg = await execute_one_drill_call(
        object(),
        case,
        tool_name="exec_show",
        params={"device_name": "atla-data-sw", "input_command": "ping 10.1.1.1"},
        reason="reach",
        device_names={"atla-data-sw"},
    )
    sync_msg = await execute_one_drill_call(
        object(),
        case,
        tool_name="check_service_sync",
        params={"device_name": "star-data-sw"},
        reason="sync",
        device_names={"star-data-sw", "atla-data-sw"},
    )
    assert "ping" in ping_msg.lower()
    assert "service_type" in sync_msg.lower()
    assert called is False
    assert case.budget.drills_used == 0


def test_select_drill_skips_all_quarantined_endpoints(monkeypatch):
    from diagnostic_mas.drill import select_drill_issues

    monkeypatch.setattr(
        "diagnostic_mas.drill._quarantined_device_map",
        lambda: {"star-data-sw": "timeout", "wash-data-sw": "timeout"},
    )
    case = CaseFile(
        budget=Budget(
            max_deep_checks=0, max_handoffs=0, max_drill_issues=2, max_tools_per_drill=4
        )
    )
    open_issue(
        case,
        code="bgp_session_down",
        message="star↔atla",
        evidence_ids=[],
        layer="routing",
        devices=["star-data-sw", "wash-data-sw"],
        edge_id="bgp:star:wash",
    )
    open_issue(
        case,
        code="bgp_session_down",
        message="atla↔clem",
        evidence_ids=[],
        layer="routing",
        devices=["atla-data-sw", "clem-data-sw"],
        edge_id="bgp:atla:clem",
    )
    picked = select_drill_issues(case, limit=2)
    assert len(picked) == 1
    assert picked[0]["edge_id"] == "bgp:atla:clem"


def test_compact_drill_context_lists_unavailable(monkeypatch):
    from diagnostic_mas.drill import compact_drill_context

    monkeypatch.setattr(
        "diagnostic_mas.drill._quarantined_device_map",
        lambda: {"star-data-sw": "timeout"},
    )
    case = CaseFile(
        budget=Budget(max_deep_checks=0, max_handoffs=0),
        device_names=["star-data-sw", "atla-data-sw"],
    )
    issue = {
        "id": "is_1",
        "code": "bgp_session_down",
        "message": "x",
        "status": "open",
        "devices": ["star-data-sw", "atla-data-sw"],
    }
    ctx = compact_drill_context(case, focus_issue=issue, remaining_tools=5)
    assert ctx["unavailable_devices"] == ["star-data-sw"]
    assert "atla-data-sw" in ctx["available_devices"]
    assert ctx["focus_live_devices"] == ["atla-data-sw"]


@pytest.mark.asyncio
async def test_acceptance_l2_dn_pinned_summary(monkeypatch):
    """Drill phase + pinned summary for L2 DN (mocked)."""
    from diagnostic_mas.drill import execute_one_drill_call, run_drill_phase
    from diagnostic_mas.report import render_report
    from diagnostic_mas.roles.summary import summary_narrative

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=2))
    open_issue(
        case,
        code="service_degraded",
        message="l2ptp x: degraded — renc Hu0/0/0/0.100 AC DN",
        evidence_ids=[],
        layer="services",
        edge_id="x",
        devices=["renc-data-sw", "lbnl-data-sw"],
        live_l2={
            "summary": "degraded",
            "endpoints": [
                {"device": "lbnl-data-sw", "ac": "Hu0/0/0/17.100", "st": "UP"},
                {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "DN"},
            ],
        },
    )

    async def fake_deep(client, plan):
        return [
            {
                "check": "exec_show",
                "result": {"admin": "up", "oper": "down"},
            }
        ]

    monkeypatch.setattr("diagnostic_mas.drill.run_deep_checks", fake_deep)

    async def fake_loop(mcp_client, settings, case, *, device_names):
        from diagnostic_mas.case import DrillSession

        session = DrillSession(max_tools=case.budget.max_tools_per_drill)
        await execute_one_drill_call(
            mcp_client,
            case,
            tool_name="exec_show",
            params={
                "device_name": "renc-data-sw",
                "input_command": "interfaces Hu0/0/0/0.100",
            },
            reason="pin AC oper state",
            device_names=device_names,
            session=session,
        )
        return 1

    class S:
        fabric_api_key = "k"
        fabric_model = "m"

    await run_drill_phase(
        object(),
        S(),
        case,
        device_names={"renc-data-sw", "lbnl-data-sw"},
        skip_llm=False,
        tool_loop_fn=fake_loop,
    )
    assert case.budget.drills_used <= 2
    assert any(e.get("kind") == "drill" for e in case.evidence)

    text = await summary_narrative(
        case,
        S(),
        skip_llm=False,
        llm_summary_fn=lambda _s, _c: (
            "L2 AC issue on renc.\n\n"
            "- **l2ptp bad** — dataplane=degraded\n"
            "  - Cause: renc Hu0/0/0/0.100 AC DN\n"
            "  - Next: verify attachment on renc\n"
        ),
    )
    assert "dataplane=degraded" in text
    assert "renc Hu0/0/0/0.100" in text
    assert "Suggested remedies (hypotheses)" not in text
    assert "Pinned findings & fix steps" not in text
    report = render_report(case, summary=text)
    assert "drill" in report.lower()
    assert "## Run details" in report


def test_build_parser_drill_budget_defaults():
    from diagnostic_mas.run import build_parser

    ns = build_parser().parse_args(["--skip-llm"])
    assert ns.max_drill_issues == 2
    assert ns.max_tools_per_drill == 12
    ns0 = build_parser().parse_args(["--skip-llm", "--max-drill-issues", "0"])
    assert ns0.max_drill_issues == 0
    ns_tools = build_parser().parse_args(
        ["--skip-llm", "--max-tools-per-drill", "15"]
    )
    assert ns_tools.max_tools_per_drill == 15


def test_heuristic_live_l2_plans_target_dn_ac():
    from diagnostic_mas.drill import heuristic_live_l2_drill_plans

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=2))
    open_issue(
        case,
        code="service_degraded",
        message="degraded",
        evidence_ids=[],
        layer="services",
        edge_id="x",
        devices=["renc-data-sw", "lbnl-data-sw"],
        live_l2={
            "summary": "degraded",
            "endpoints": [
                {"device": "lbnl-data-sw", "ac": "Hu0/0/0/17.100", "st": "UP"},
                {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "DN"},
            ],
        },
    )
    plans = heuristic_live_l2_drill_plans(case, remaining=2)
    assert len(plans) >= 1
    assert plans[0]["check"] == "exec_show"
    assert plans[0]["args"]["device_name"] == "renc-data-sw"
    assert "Hu0/0/0/0.100" in plans[0]["args"]["input_command"]
    # Second plan: xconnect on DN device when budget allows
    if len(plans) >= 2:
        assert plans[1]["args"]["device_name"] == "renc-data-sw"
        assert "l2vpn xconnect" in plans[1]["args"]["input_command"]


@pytest.mark.asyncio
async def test_run_drill_phase_falls_back_when_tool_loop_empty(monkeypatch):
    from diagnostic_mas.drill import run_drill_phase

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=2))
    open_issue(
        case,
        code="service_degraded",
        message="degraded",
        evidence_ids=[],
        layer="services",
        edge_id="x",
        devices=["renc-data-sw"],
        live_l2={
            "summary": "degraded",
            "endpoints": [
                {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "DN"},
            ],
        },
    )

    async def empty_loop(*a, **k):
        return 0

    async def fake_deep(client, plan):
        return [{"check": t["check"], "result": {"ok": True}} for t in plan]

    monkeypatch.setattr("diagnostic_mas.drill.run_deep_checks", fake_deep)

    class S:
        fabric_api_key = "k"
        fabric_model = "m"

    await run_drill_phase(
        object(),
        S(),
        case,
        device_names={"renc-data-sw"},
        skip_llm=False,
        tool_loop_fn=empty_loop,
    )
    assert case.budget.drills_used >= 1
    assert any(e.get("kind") == "drill" for e in case.evidence)


def test_pinned_prompt_uses_observed_cause_fix():
    from diagnostic_mas.roles.summary import _system_prompt_for_summary

    text = _system_prompt_for_summary(pinned=True)
    assert "SHORT executive skim" in text
    assert "Cause:" in text
    assert "Next:" in text
    assert "dataplane=" in text
    assert "Do NOT use Observed:" in text or "do not paste long observed" in text.lower()
    assert "changes_since_previous" in text
    assert "newly failed" in text.lower() or "recovered devices" in text.lower()
    assert "Pinned findings & fix steps" not in text
    assert "remote" in text.lower() or "None" in text
    assert "local id" in text.lower() or "local ids" in text.lower()


def test_format_live_l2_cause_labels_xc_not_ac_only():
    from diagnostic_mas.roles.service import format_live_l2_cause

    text = format_live_l2_cause(
        {
            "endpoints": [
                {
                    "device": "renc-data-sw",
                    "ac": "Hu0/0/0/0.100",
                    "st": "DN",
                    "ac_st": "UP",
                    "seg2_st": "DN",
                    "seg2": "EVPN 9003,99,None",
                }
            ]
        }
    )
    assert "XC DN" in text
    assert "AC UP" in text
    assert "EVPN 9003,99,None" in text
    assert "AC DN" not in text


def test_report_omits_drill_findings_when_summary_pinned():
    from diagnostic_mas.case import Budget, CaseFile, add_evidence
    from diagnostic_mas.report import render_report

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_evidence(
        case,
        {
            "kind": "drill_finding",
            "role": "drill",
            "payload": {
                "observed": "xc dn",
                "cause": "remote none",
                "fix_suggestion": "set remote",
            },
        },
    )
    summary = (
        "L2PTP issue on renc.\n\n"
        "- **l2ptp svc** — dataplane=down\n"
        "  - Cause: remote none\n"
        "  - Next: set remote\n"
    )
    text = render_report(case, summary=summary)
    assert "## Summary" in text
    assert "dataplane=down" in text
    assert "## Drill findings" not in text


def test_find_iface_up_xconnect_dn_targets():
    from diagnostic_mas.drill import find_iface_up_xconnect_dn_targets

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_drill_issues=2, max_tools_per_drill=5))
    open_issue(
        case,
        code="service_degraded",
        message="degraded",
        evidence_ids=[],
        layer="services",
        edge_id="x",
        devices=["renc-data-sw", "lbnl-data-sw"],
        live_l2={
            "summary": "degraded",
            "endpoints": [
                {"device": "lbnl-data-sw", "ac": "Hu0/0/0/17.100", "st": "UP"},
                {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "DN"},
            ],
        },
    )
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
                "result": (
                    "Hu0/0/0/0.100 is up, line protocol is up\n"
                    "  Encapsulation 802.1Q Virtual LAN"
                ),
            },
        },
    )
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
                    "input_command": "l2vpn xconnect",
                },
                "result": (
                    "Legend: ST = Up\n"
                    "evpn_vpws  evpn_vpws_9001\n"
                    "  DN   Hu0/0/0/0.100    DN\n"
                ),
            },
        },
    )
    targets = find_iface_up_xconnect_dn_targets(case)
    assert targets
    assert targets[0]["device"] == "renc-data-sw"
    assert targets[0]["ac"] == "Hu0/0/0/0.100"


@pytest.mark.asyncio
async def test_wave2_fallback_after_iface_up_xc_dn(monkeypatch):
    from diagnostic_mas.drill import run_drill_phase

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_drill_issues=2, max_tools_per_drill=5))
    open_issue(
        case,
        code="service_degraded",
        message="degraded",
        evidence_ids=[],
        layer="services",
        edge_id="x",
        devices=["renc-data-sw", "lbnl-data-sw"],
        live_l2={
            "summary": "degraded",
            "endpoints": [
                {"device": "lbnl-data-sw", "ac": "Hu0/0/0/17.100", "st": "UP"},
                {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "DN"},
            ],
        },
    )

    async def empty_loop(*a, **k):
        return 0

    async def fake_deep(client, plan):
        out = []
        for t in plan:
            cmd = str((t.get("args") or {}).get("input_command") or "")
            if cmd.startswith("interfaces Hu0/0/0/0.100"):
                out.append(
                    {
                        "check": "exec_show",
                        "result": "Hu0/0/0/0.100 is up, line protocol is up\n",
                    }
                )
            elif "l2vpn xconnect" in cmd and (t.get("args") or {}).get(
                "device_name"
            ) == "renc-data-sw":
                out.append(
                    {
                        "check": "exec_show",
                        "result": (
                            "evpn_vpws  evpn_vpws_9001\n"
                            "  DN   Hu0/0/0/0.100    DN\n"
                        ),
                    }
                )
            else:
                out.append({"check": "exec_show", "result": "ok"})
        return out

    monkeypatch.setattr("diagnostic_mas.drill.run_deep_checks", fake_deep)

    class S:
        fabric_api_key = "k"
        fabric_model = "m"

    await run_drill_phase(
        object(),
        S(),
        case,
        device_names={"renc-data-sw", "lbnl-data-sw"},
        skip_llm=False,
        tool_loop_fn=empty_loop,
    )
    # wave1 heuristic (2) + wave2 (peer/bgp/isis) within max 5
    assert case.budget.drills_used >= 3
    cmds = []
    for e in case.evidence:
        if e.get("kind") != "drill":
            continue
        p = e.get("payload") or {}
        args = p.get("args") or {}
        if args.get("input_command"):
            cmds.append(str(args["input_command"]))
    assert any("bgp" in c for c in cmds) or any("isis" in c for c in cmds)


def test_select_drill_issues_caps_and_prefers_live_l2_dn():
    from diagnostic_mas.drill import select_drill_issues

    case = CaseFile(
        budget=Budget(
            max_deep_checks=0, max_handoffs=0, max_drill_issues=2, max_tools_per_drill=30
        )
    )
    open_issue(
        case,
        code="other",
        message="bgp flap",
        evidence_ids=[],
        severity="low",
        layer="routing",
    )
    open_issue(
        case,
        code="service_degraded",
        message="l2 a",
        evidence_ids=[],
        severity="high",
        layer="services",
        edge_id="a",
        live_l2={
            "endpoints": [
                {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "DN"},
            ]
        },
    )
    open_issue(
        case,
        code="service_degraded",
        message="l2 b",
        evidence_ids=[],
        severity="high",
        layer="services",
        edge_id="b",
        live_l2={
            "endpoints": [
                {"device": "lbnl-data-sw", "ac": "Hu0/0/0/1.100", "st": "DN"},
            ]
        },
    )
    open_issue(
        case,
        code="service_degraded",
        message="l2 c skipped",
        evidence_ids=[],
        severity="high",
        layer="services",
        edge_id="c",
        live_l2={
            "endpoints": [
                {"device": "sunn-data-sw", "ac": "Hu0/0/0/2.100", "st": "DN"},
            ]
        },
    )
    picked = select_drill_issues(case, limit=2)
    assert len(picked) == 2
    assert all(
        (p.get("live_l2") or {}).get("endpoints") for p in picked
    )


def test_select_drill_prefers_open_over_budget_exhausted():
    from diagnostic_mas.case import set_issue_status
    from diagnostic_mas.drill import select_drill_issues

    case = CaseFile(
        budget=Budget(
            max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=12
        )
    )
    exhausted = open_issue(
        case,
        code="service_degraded",
        message="old exhausted",
        evidence_ids=[],
        severity="high",
        layer="services",
        edge_id="exhausted-svc",
        live_l2={
            "endpoints": [
                {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "DN"},
            ]
        },
    )
    set_issue_status(case, exhausted, "budget_exhausted")
    open_issue(
        case,
        code="other",
        message="still open",
        evidence_ids=[],
        severity="low",
        layer="routing",
        edge_id="open-bgp",
    )
    picked = select_drill_issues(case, limit=1)
    assert len(picked) == 1
    assert picked[0].get("edge_id") == "open-bgp"
    assert picked[0].get("status") == "open"

    # When nothing open remains, budget_exhausted may still be drilled.
    case.issues = [i for i in case.issues if i.get("status") == "budget_exhausted"]
    picked2 = select_drill_issues(case, limit=1)
    assert len(picked2) == 1
    assert picked2[0].get("status") == "budget_exhausted"


def test_select_drill_issues_skips_inventory_neighbor_codes():
    """Unmapped IS-IS/BGP neighbors stay on Issues but are not drilled."""
    from diagnostic_mas.drill import select_drill_issues

    case = CaseFile(
        budget=Budget(
            max_deep_checks=0, max_handoffs=0, max_drill_issues=2, max_tools_per_drill=12
        )
    )
    open_issue(
        case,
        code="unknown_neighbor_system_id",
        message="uky-data-sw Hu0/0/0/23.856: unmapped IS-IS system id 'star-data-sw'",
        evidence_ids=[],
        severity="medium",
        layer="routing",
    )
    open_issue(
        case,
        code="unknown_neighbor_address",
        message="lbnl-data-sw 10.148.0.1: could not map neighbor address to NSO device",
        evidence_ids=[],
        severity="medium",
        layer="routing",
    )
    open_issue(
        case,
        code="service_down",
        message="l2sts broken",
        evidence_ids=[],
        severity="high",
        layer="services",
        edge_id="l2-sts-1",
        live_l2={
            "endpoints": [
                {"device": "lbnl-data-sw", "ac": "TF0/0/0/23/1.100", "error": "ac_not_found"},
            ]
        },
    )
    picked = select_drill_issues(case, limit=2)
    assert len(picked) == 1
    assert picked[0]["code"] == "service_down"
    assert picked[0]["edge_id"] == "l2-sts-1"

    # Only inventory noise → nothing to drill
    case2 = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    open_issue(
        case2,
        code="unknown_neighbor_address",
        message="renc-data-sw 10.148.0.1: could not map",
        evidence_ids=[],
        layer="routing",
    )
    assert select_drill_issues(case2, limit=2) == []


def test_drill_agent_prompt_remote_and_cli():
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "diagnostic_mas/prompts/drill_agent.txt"
    text = p.read_text(encoding="utf-8")
    assert "peer-loopback" in text or "peer loopback" in text.lower()
    assert "segment 2 evpn" in text
    assert "itself" in text.lower() or "own address" in text.lower()
    assert "already_run_probes" in text
    assert "prior_exploration" in text


def test_prior_exploration_lists_dataplane_probes_and_filters_plans():
    from diagnostic_mas.case import add_diagnosis, set_issue_status
    from diagnostic_mas.drill import (
        _filter_plans_already_run,
        compact_drill_context,
        prior_exploration_for_issue,
    )

    case = CaseFile(
        budget=Budget(
            max_deep_checks=0, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=8
        )
    )
    iid = open_issue(
        case,
        code="service_degraded",
        message="budget hit mid-verify",
        evidence_ids=[],
        severity="high",
        layer="services",
        edge_id="svc-a",
        devices=["renc-data-sw", "sunn-data-sw"],
        live_l2={
            "endpoints": [
                {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "DN"},
                {"device": "sunn-data-sw", "ac": "Hu0/0/0/2.100", "st": "UP"},
            ]
        },
    )
    set_issue_status(case, iid, "budget_exhausted")
    add_evidence(
        case,
        {
            "kind": "drill",
            "role": "dataplane",
            "layer": "services",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "renc-data-sw",
                    "input_command": "l2vpn xconnect",
                },
                "issue_edge_id": "svc-a",
                "result": "XC DN",
            },
        },
    )
    add_evidence(
        case,
        {
            "kind": "dataplane_incomplete",
            "role": "dataplane",
            "layer": "services",
            "payload": {
                "name": "svc-a",
                "observed": "ran xconnect on renc only",
                "cause": "tool budget exhausted",
                "dataplane_status": "unknown",
                "source": "budget",
            },
        },
    )
    add_diagnosis(
        case,
        kind="dataplane",
        status="unknown",
        source="budget",
        observed="ran xconnect on renc only",
        cause="tool budget exhausted",
        subject={"name": "svc-a"},
        evidence_ids=[],
        extra={"complete": False},
    )
    issue = next(i for i in case.issues if i["id"] == iid)
    prior = prior_exploration_for_issue(case, issue)
    assert "exec_show renc-data-sw l2vpn xconnect" in prior["already_run_probes"]
    assert prior["diagnoses"]
    assert prior["diagnoses"][0]["complete"] is False
    assert any(
        t.get("kind") == "dataplane_incomplete" for t in prior["prior_tool_traces"]
    )

    ctx = compact_drill_context(case, focus_issue=issue, remaining_tools=8)
    assert "prior_exploration" in ctx
    assert ctx["prior_exploration"]["already_run_probes"]

    plans = [
        {
            "check": "exec_show",
            "args": {
                "device_name": "renc-data-sw",
                "input_command": "l2vpn xconnect",
            },
        },
        {
            "check": "exec_show",
            "args": {
                "device_name": "sunn-data-sw",
                "input_command": "l2vpn xconnect",
            },
        },
    ]
    kept = _filter_plans_already_run(case, plans, focus_issue=issue)
    assert len(kept) == 1
    assert kept[0]["args"]["device_name"] == "sunn-data-sw"
