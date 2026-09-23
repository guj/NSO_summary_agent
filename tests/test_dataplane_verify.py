"""Tests for LLM dataplane verify selection / apply."""

from __future__ import annotations

import json

import pytest

from diagnostic_mas.case import Budget, CaseFile, DrillSession, add_evidence
from diagnostic_mas.dataplane_verify import (
    coerce_dataplane_mcp_call,
    dataplane_system_prompt,
    disallowed_dataplane_show_command,
    disallowed_explore_nso_path,
    empty_mcp_tool_name_error,
    fallback_dataplane_status,
    parse_dataplane_conclusion,
    select_dataplane_candidates,
)


def test_dataplane_system_prompt_by_service_type(tmp_path, monkeypatch):
    from diagnostic_mas import dataplane_verify as dv
    monkeypatch.setattr(dv, "_PROMPTS", tmp_path)
    (tmp_path / "dataplane_agent.txt").write_text("generic instructions")
    for kind in ("l2sts", "l2ptp", "l3rt"):
        (tmp_path / f"dataplane_agent_{kind}.txt").write_text(f"{kind} instructions")
        assert dv.dataplane_system_prompt(kind) == f"{kind} instructions"
    assert dv.dataplane_system_prompt("unsupported") == "generic instructions"
    assert dv.dataplane_system_prompt(None) == "generic instructions"


def test_coerce_dataplane_mcp_call_infers_exec_show():
    name, params, note = coerce_dataplane_mcp_call(
        "",
        {
            "device_name": "amst-data-sw",
            "input_command": "interfaces HundredGigE0/0/0/5.2036",
        },
    )
    assert name == "exec_show"
    assert params["input_command"].startswith("interfaces")
    assert note and "exec_show" in note

    name2, params2, note2 = coerce_dataplane_mcp_call(
        "",
        {"tool_name": "get_interface_health", "device_name": "amst-data-sw"},
    )
    assert name2 == "get_interface_health"
    assert "tool_name" not in params2
    assert note2 and "lifted" in note2

    name3, params3, note3 = coerce_dataplane_mcp_call("", {"service_type": "l3rt"})
    assert name3 == ""
    assert note3 is None
    err = empty_mcp_tool_name_error(params3)
    assert "non-empty tool_name" in err
    assert "exec_show" in err


def test_disallowed_bare_show_evpn():
    assert disallowed_dataplane_show_command("evpn evi summary")
    assert disallowed_dataplane_show_command("show evpn route-target")
    assert disallowed_dataplane_show_command("show evpn bridge-domain detail")
    assert disallowed_dataplane_show_command("show l2vpn evpn evi 9001")
    assert disallowed_dataplane_show_command("l2vpn evpn evi 9001 detail")
    assert disallowed_dataplane_show_command("show l2vpn evpn summary")
    bare = disallowed_dataplane_show_command("evpn evi 9001")
    assert bare and "get_device_config" in bare
    assert "bgp l2vpn evpn rd" in bare
    assert "Config alone is not enough" in bare
    assert "conclude_dataplane now" not in bare
    evi_detail = disallowed_dataplane_show_command("evpn evi vpn-id 9037 detail")
    assert evi_detail and "bgp l2vpn evpn rd" in evi_detail
    assert disallowed_dataplane_show_command("bgp l2vpn evpn") is None
    assert disallowed_dataplane_show_command("show bgp l2vpn evpn summary") is None
    assert disallowed_dataplane_show_command("bgp l2vpn evpn rd 10.1.1.1:9037") is None
    assert disallowed_dataplane_show_command("l2vpn bridge-domain brief") is None
    assert disallowed_dataplane_show_command("run l2vpn") is None
    run_evpn = disallowed_dataplane_show_command("run evpn")
    assert run_evpn and "get_device_config" in run_evpn
    assert "conclude" in run_evpn.lower()
    assert "not sufficient evidence" in run_evpn.lower()
    assert "dataplane_status=unknown" in run_evpn.lower()
    assert disallowed_dataplane_show_command("run | include route-target")


@pytest.mark.asyncio
async def test_soft_block_run_evpn_early_keeps_recovery_path(monkeypatch):
    """Early soft-block of run evpn must not force conclude before ladder."""
    from types import SimpleNamespace as NS

    from diagnostic_mas import dataplane_verify as dv
    from nso_facts.mcp_client import start_mcp_cache, stop_mcp_cache

    class Client:
        async def list_tools(self):
            return [
                NS(
                    name="exec_show",
                    description="Show",
                    inputSchema={
                        "type": "object",
                        "properties": {"device_name": {"type": "string"}},
                    },
                ),
                NS(
                    name="get_device_config",
                    description="Config",
                    inputSchema={
                        "type": "object",
                        "properties": {"device_name": {"type": "string"}},
                    },
                ),
            ]

    class LLM:
        def __init__(self):
            self.chat = self.completions = self
            self.calls = 0
            self.saw_recovery_nudge = False
            self.round3_had_mcp = False

        def create(self, **kwargs):
            self.calls += 1
            tools = kwargs.get("tools") or []
            names = [t["function"]["name"] for t in tools]
            msgs = kwargs.get("messages") or []
            if any(
                "get_device_config / 'bgp l2vpn evpn" in str(m.get("content") or "")
                or "Rejected/forbidden shows do not count" in str(
                    m.get("content") or ""
                )
                for m in msgs
                if isinstance(m, dict)
            ):
                self.saw_recovery_nudge = True
            if self.calls == 1:
                call = NS(
                    id="1",
                    function=NS(
                        name="mcp_call",
                        arguments=json.dumps(
                            {
                                "tool_name": "exec_show",
                                "params": {
                                    "device_name": "utah-data-sw",
                                    "input_command": "run evpn",
                                },
                                "reason": "RT",
                            }
                        ),
                    ),
                )
                return NS(choices=[NS(message=NS(content=None, tool_calls=[call]))])
            if self.calls == 2:
                assert "mcp_call" in names  # not conclude_only yet
                call = NS(
                    id="2",
                    function=NS(
                        name="mcp_call",
                        arguments=json.dumps(
                            {
                                "tool_name": "get_device_config",
                                "params": {"device_name": "utah-data-sw"},
                                "reason": "EVI/RT",
                            }
                        ),
                    ),
                )
                return NS(choices=[NS(message=NS(content=None, tool_calls=[call]))])
            if self.calls == 3:
                assert "mcp_call" in names
                self.round3_had_mcp = True
                call = NS(
                    id="3",
                    function=NS(
                        name="conclude_dataplane",
                        arguments=json.dumps(
                            {
                                "dataplane_status": "unknown",
                                "observed": "AC/BD up; RT path incomplete",
                                "cause": "verification gap",
                                "confidence": "low",
                            }
                        ),
                    ),
                )
                return NS(choices=[NS(message=NS(content=None, tool_calls=[call]))])
            raise AssertionError(f"unexpected LLM round {self.calls}")

    async def execute(*args, **kwargs):
        return "device config: evi 9037"

    monkeypatch.setattr(dv, "execute_one_drill_call", execute)
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    record = {
        "name": "P4_KANS_NET",
        "service_type": "l2sts",
        "system_status": "up",
        "devices": ["utah-data-sw", "kans-data-sw"],
    }
    llm = LLM()
    start_mcp_cache()
    try:
        await dv.llm_dataplane_verify_one(
            Client(),
            NS(fabric_model="test"),
            case,
            record=record,
            device_names={"utah-data-sw", "kans-data-sw"},
            session=DrillSession(max_tools=10),
            openai_client=llm,
        )
    finally:
        stop_mcp_cache()
    assert llm.saw_recovery_nudge
    assert llm.round3_had_mcp
    assert llm.calls == 3


@pytest.mark.asyncio
async def test_soft_block_run_evpn_after_ladder_forces_conclude(monkeypatch):
    """nso21: after config+BGP, blocked run evpn must conclude — not fish further."""
    from types import SimpleNamespace as NS

    from diagnostic_mas import dataplane_verify as dv
    from nso_facts.mcp_client import start_mcp_cache, stop_mcp_cache

    class Client:
        async def list_tools(self):
            return [
                NS(
                    name="exec_show",
                    description="Show",
                    inputSchema={
                        "type": "object",
                        "properties": {"device_name": {"type": "string"}},
                    },
                ),
                NS(
                    name="get_device_config",
                    description="Config",
                    inputSchema={
                        "type": "object",
                        "properties": {"device_name": {"type": "string"}},
                    },
                ),
            ]

    class LLM:
        def __init__(self):
            self.chat = self.completions = self
            self.calls = 0
            self.saw_force_conclude = False
            self.round3_conclude_only = False

        def create(self, **kwargs):
            self.calls += 1
            tools = kwargs.get("tools") or []
            names = [t["function"]["name"] for t in tools]
            msgs = kwargs.get("messages") or []
            if any(
                "sufficient evidence" in str(m.get("content") or "").lower()
                and "dataplane_status=unknown" in str(m.get("content") or "").lower()
                for m in msgs
                if isinstance(m, dict)
            ):
                self.saw_force_conclude = True
            if self.calls == 1:
                # Ladder already present via prior partials injected below —
                # first model action is late run evpn.
                call = NS(
                    id="1",
                    function=NS(
                        name="mcp_call",
                        arguments=json.dumps(
                            {
                                "tool_name": "exec_show",
                                "params": {
                                    "device_name": "wash-data-sw",
                                    "input_command": "run evpn",
                                },
                                "reason": "RT",
                            }
                        ),
                    ),
                )
                return NS(choices=[NS(message=NS(content=None, tool_calls=[call]))])
            if self.calls == 2:
                # Must be conclude-only (no mcp_call).
                assert names == ["conclude_dataplane"] or "mcp_call" not in names
                self.round3_conclude_only = "mcp_call" not in names
                call = NS(
                    id="2",
                    function=NS(
                        name="conclude_dataplane",
                        arguments=json.dumps(
                            {
                                "dataplane_status": "unknown",
                                "observed": "RT unverified; ladder done",
                                "cause": "verification gap — missing RT text",
                                "confidence": "medium",
                            }
                        ),
                    ),
                )
                return NS(choices=[NS(message=NS(content=None, tool_calls=[call]))])
            raise AssertionError(f"unexpected LLM round {self.calls}")

    async def execute(*_a, **_k):
        raise AssertionError("run evpn must be soft-blocked before MCP")

    monkeypatch.setattr(dv, "execute_one_drill_call", execute)
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    record = {
        "name": "L2STS_MAX_WASH",
        "service_type": "l2sts",
        "system_status": "up",
        "devices": ["wash-data-sw", "max-data-sw"],
    }
    llm = LLM()

    # Pre-seed partial ladder by wrapping verify_one's partial list is hard;
    # instead monkeypatch the ladder helper to True for this dig.
    monkeypatch.setattr(dv, "_partial_has_l2sts_rt_ladder", lambda _p: True)

    start_mcp_cache()
    try:
        await dv.llm_dataplane_verify_one(
            Client(),
            NS(fabric_model="test"),
            case,
            record=record,
            device_names={"wash-data-sw", "max-data-sw"},
            session=DrillSession(max_tools=10),
            openai_client=llm,
        )
    finally:
        stop_mcp_cache()
    assert llm.saw_force_conclude
    assert llm.round3_conclude_only
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_soft_block_after_error_keeps_tool_budget(monkeypatch):
    """Legacy name: early soft-block still allows recovery mcp_call."""
    await test_soft_block_run_evpn_early_keeps_recovery_path(monkeypatch)


def test_disallowed_bridge_domain_mac_suffix():
    assert disallowed_dataplane_show_command(
        "l2vpn bridge-domain bd-name bd-l2-STS-25b0e3a0-364b-4b0 mac"
    )
    assert disallowed_dataplane_show_command(
        "show l2vpn bridge-domain bd-name bd-l2-STS-25b0e3a0-364b-4b0 mac-address"
    )
    assert disallowed_dataplane_show_command(
        "l2vpn bridge-domain bd-name bd-l2-STS-25b0e3a0-364b-4b0 mac learned"
    )
    # Valid alternatives must still pass
    assert (
        disallowed_dataplane_show_command(
            "l2vpn bridge-domain bd-name bd-l2-STS-25b0e3a0-364b-4b0 detail"
        )
        is None
    )
    assert (
        disallowed_dataplane_show_command(
            "l2vpn forwarding bridge-domain mac-address location 0/RP0/CPU0"
        )
        is None
    )


def test_disallowed_incomplete_forwarding_mac_and_bgp_evi():
    assert disallowed_dataplane_show_command(
        "l2vpn forwarding bridge-domain mac-address"
    )
    assert "location" in disallowed_dataplane_show_command(
        "show l2vpn forwarding bridge-domain mac-address"
    )
    assert disallowed_dataplane_show_command("bgp l2vpn evpn evi 9001")
    assert disallowed_dataplane_show_command("show bgp l2vpn evpn evi 9001 detail")
    assert disallowed_dataplane_show_command("bgp l2vpn evpn summary") is None
    assert disallowed_dataplane_show_command("bgp l2vpn evpn detail") is None


def test_disallowed_explore_nso_path():
    assert disallowed_explore_nso_path("")
    assert disallowed_explore_nso_path("/services/l2sts/foo")
    assert disallowed_explore_nso_path("services/l2sts/foo")
    assert disallowed_explore_nso_path("devices/device=renc-data-sw")
    assert disallowed_explore_nso_path("/devices/device=renc-data-sw")
    ok = disallowed_explore_nso_path("tailf-ncs:devices/device=renc-data-sw")
    assert ok is None
    assert (
        disallowed_explore_nso_path("/tailf-ncs:devices/device=renc-data-sw")
        is None
    )
    assert disallowed_explore_nso_path("tailf-ned-cisco-ios-xr:l2vpn/evpn")
    assert disallowed_explore_nso_path(
        "tailf-ncs:devices/device=lbnl-data-sw/config/"
        "tailf-ned-cisco-ios-xr:l2vpn/evpn"
    )
    assert "get_device_config" in disallowed_explore_nso_path(
        "tailf-ncs:devices/device=renc-data-sw/config/"
        "tailf-ned-cisco-ios-xr:l2vpn/evpn"
    )


def test_select_dataplane_candidates_system_up_not_checked():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    services = {
        "l2sts/a": {
            "name": "a",
            "service_type": "l2sts",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
        },
        "l2sts/b": {
            "name": "b",
            "service_type": "l2sts",
            "system_status": "up",
            "dataplane_status": "up",
            "status": "up",
        },
        "l2sts/c": {
            "name": "c",
            "service_type": "l2sts",
            "system_status": "degraded",
            "dataplane_status": "not_checked",
            "status": "degraded",
        },
    }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {"extra": {"services": services}},
        },
    )
    picked = select_dataplane_candidates(case, limit=5, one_per_typed_category=False)
    assert [r.get("name") for _e, r in picked] == ["a"]


def test_select_dataplane_one_per_typed_category():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    services = {
        "l2sts/a": {
            "name": "sts-a",
            "service_type": "l2sts",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
        },
        "l2sts/b": {
            "name": "sts-b",
            "service_type": "l2sts",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
        },
        "l2ptp/c": {
            "name": "ptp-c",
            "service_type": "l2ptp",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
        },
        "l3rt/d": {
            "name": "l3-d",
            "service_type": "l3rt",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
        },
        "custom/e": {
            "name": "custom-e",
            "service_type": "custom-no-prompt",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
        },
    }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {"extra": {"services": services}},
        },
    )
    picked = select_dataplane_candidates(case)  # multi-type: one each
    names = {r.get("name") for _e, r in picked}
    assert names == {"sts-a", "ptp-c", "l3-d"}
    types = {r.get("service_type") for _e, r in picked}
    assert types == {"l2sts", "l2ptp", "l3rt"}


def test_select_dataplane_includes_basic_passed_typed_l2ptp():
    """Clearly-up live_l2 l2ptp still gets the typed-category LLM sample."""
    from diagnostic_mas.dataplane_verify import apply_coverage_after_candidate_select

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    services = {
        "l2sts/sts": {
            "name": "l2-sts",
            "service_type": "l2sts",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
            "live_l2": {
                "endpoints": [
                    {"device": "lbnl-data-sw", "ac": "Hu0/0/0/4.100", "st": "UP"},
                    {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "UP"},
                ]
            },
        },
        "l2ptp/a": {
            "name": "l2-PTP-healthy",
            "service_type": "l2ptp",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
            "live_l2": {
                "endpoints": [
                    {"device": "lbnl-data-sw", "ac": "Hu0/0/0/17.100", "st": "UP"},
                    {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "UP"},
                ]
            },
        },
        "l2ptp/b": {
            "name": "fabric-l2ptp-t1",
            "service_type": "l2ptp",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
            "live_l2": {
                "endpoints": [
                    {"device": "lbnl-data-sw", "ac": "Hu0/0/0/9.1001", "st": "UP"},
                    {"device": "renc-data-sw", "ac": "Hu0/0/0/0.1002", "st": "UP"},
                ]
            },
        },
        "l3rt/d": {
            "name": "l3-a",
            "service_type": "l3rt",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
        },
    }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {"extra": {"services": services}},
        },
    )
    from diagnostic_mas.dataplane_verify import init_service_coverage

    init_service_coverage(case)
    assert case.service_coverage["l2-PTP-healthy"] == "basic_passed"
    assert case.service_coverage["fabric-l2ptp-t1"] == "basic_passed"
    ordinary = select_dataplane_candidates(case, suspicious_only=True)
    assert "l2-PTP-healthy" not in {r["name"] for _, r in ordinary}
    explicit = select_dataplane_candidates(
        case, suspicious_only=True, explicit_service=True, per_category=10
    )
    assert "l2-PTP-healthy" in {r["name"] for _, r in explicit}
    picked = select_dataplane_candidates(case)
    types = {r.get("service_type") for _e, r in picked}
    assert "l2ptp" in types
    assert "l2sts" in types
    assert "l3rt" in types
    apply_coverage_after_candidate_select(case, picked)
    picked_names = {r.get("name") for _e, r in picked}
    for name, cov in case.service_coverage.items():
        if name in picked_names:
            continue
        if name in {"l2-PTP-healthy", "fabric-l2ptp-t1"}:
            assert cov == "category_peer_skipped"


def test_apply_coverage_collection_down_not_budget_skipped():
    """Collection-down instances are not dig-eligible — don't blame budget."""
    from diagnostic_mas.dataplane_verify import (
        apply_coverage_after_candidate_select,
        init_service_coverage,
        select_dataplane_candidates,
    )

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    services = {
        "l2sts/bad": {
            "name": "l2-STS-down",
            "service_type": "l2sts",
            "system_status": "down",
            "dataplane_status": "not_checked",
            "status": "down",
            "live_l2": {
                "endpoints": [
                    {"device": "lbnl-data-sw", "ac": "a", "error": "ac_not_found"},
                    {"device": "renc-data-sw", "ac": "b", "error": "no_xconnect_data"},
                ]
            },
        },
        "l2ptp/ok": {
            "name": "l2-PTP-up",
            "service_type": "l2ptp",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
            "live_l2": {
                "endpoints": [
                    {"device": "lbnl-data-sw", "ac": "Hu0/0/0/17.100", "st": "UP"},
                    {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "UP"},
                ]
            },
        },
    }
    case.evidence.append(
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {"extra": {"services": services}},
        },
    )
    init_service_coverage(case)
    assert case.service_coverage["l2-STS-down"] == "needs_investigation"
    picked = select_dataplane_candidates(case)
    assert {r.get("name") for _e, r in picked} == {"l2-PTP-up"}
    apply_coverage_after_candidate_select(case, picked)
    assert case.service_coverage["l2-STS-down"] == "collection_concluded"
    assert case.service_coverage["l2-STS-down"] != "budget_skipped"
    # Selected candidate keeps prior coverage until dig updates it.
    assert "l2-PTP-up" in {r.get("name") for _e, r in picked}


def test_select_dataplane_single_type_picks_two():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    services = {
        "l2ptp/a": {
            "name": "ptp-a",
            "service_type": "l2ptp",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
        },
        "l2ptp/b": {
            "name": "ptp-b",
            "service_type": "l2ptp",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
        },
        "l2ptp/c": {
            "name": "ptp-c",
            "service_type": "l2ptp",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
        },
    }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {"extra": {"services": services}},
        },
    )
    picked = select_dataplane_candidates(case)
    names = [r.get("name") for _e, r in picked]
    assert len(names) == 2
    assert set(names) <= {"ptp-a", "ptp-b", "ptp-c"}
    assert all(
        r.get("service_type") == "l2ptp" for _e, r in picked
    )


def test_select_dataplane_per_category_even_sample():
    """--max-dataplane-per-category N takes up to N from each typed prompt."""
    from typing import Any

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    services: dict[str, dict[str, Any]] = {}
    for stype, prefix, n in (
        ("l2ptp", "ptp", 5),
        ("l2sts", "sts", 4),
        ("l3rt", "l3", 3),
    ):
        for i in range(n):
            name = f"{prefix}-{i}"
            services[f"{stype}/{name}"] = {
                "name": name,
                "service_type": stype,
                "system_status": "up",
                "dataplane_status": "not_checked",
                "status": "up",
            }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {"extra": {"services": services}},
        },
    )
    picked = select_dataplane_candidates(case, per_category=3)
    by_type: dict[str, list[str]] = {}
    for _e, rec in picked:
        by_type.setdefault(str(rec.get("service_type")), []).append(
            str(rec.get("name"))
        )
    assert set(by_type) == {"l2ptp", "l2sts", "l3rt"}
    assert len(by_type["l2ptp"]) == 3
    assert len(by_type["l2sts"]) == 3
    assert len(by_type["l3rt"]) == 3  # only 3 eligible

    # Round-robin: first three picks are different categories (l3rt not last-only).
    first_three = [str(r.get("service_type")) for _e, r in picked[:3]]
    assert len(set(first_three)) == 3

    # Optional total cap truncates after even per-category pick.
    capped = select_dataplane_candidates(case, per_category=3, limit=5)
    assert len(capped) == 5

    # Explicit 0 skips digs.
    assert select_dataplane_candidates(case, per_category=0) == []


def test_select_dataplane_category_rotate_seed_changes_start():
    from diagnostic_mas.dataplane_verify import _category_rotation_order

    stems = ["l2ptp", "l2sts", "l3rt"]
    orders = {
        seed: _category_rotation_order(stems, seed=seed)
        for seed in ("run-a", "run-b", "run-c", "20260918T000000Z")
    }
    # At least two seeds produce a different lead category.
    leads = {tuple(o) for o in orders.values()}
    assert len(leads) >= 2
    # Every rotation still contains all categories.
    for order in orders.values():
        assert set(order) == set(stems)


def test_cli_max_dataplane_per_category_flag():
    from diagnostic_mas.run import build_parser

    ns = build_parser().parse_args(["--max-dataplane-per-category", "10"])
    assert ns.max_dataplane_per_category == 10
    assert ns.max_dataplane_services is None


def test_fleet_select_prefers_one_soft_error_per_typed_category():
    """Fleet mode: soft-error preferred for the typed slot, not all of them."""
    from diagnostic_mas.dataplane_verify import (
        apply_coverage_after_candidate_select,
        init_service_coverage,
    )

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    services = {
        "l3rt/x": {
            "name": "l3-a",
            "service_type": "l3rt",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
        },
        "l2sts/soft-1": {
            "name": "l2-sts-soft-1",
            "service_type": "l2sts",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
            "live_l2": {
                "summary": "unknown",
                "endpoints": [
                    {"error": "ac_not_found", "ac": "a"},
                    {"error": "ac_not_found", "ac": "b"},
                ],
            },
        },
        "l2sts/soft-2": {
            "name": "l2-sts-soft-2",
            "service_type": "l2sts",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
            "live_l2": {
                "endpoints": [
                    {"error": "no_xconnect_data", "ac": "c"},
                    {"error": "ac_not_found", "ac": "d"},
                ],
            },
        },
        "l2sts/ok": {
            "name": "l2-sts-ok",
            "service_type": "l2sts",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
            "live_l2": {
                "endpoints": [
                    {"device": "a", "ac": "Hu0/0/0/1.100", "st": "UP"},
                    {"device": "b", "ac": "Hu0/0/0/2.100", "st": "UP"},
                ],
            },
        },
        "l2ptp/ok": {
            "name": "l2-ptp-ok",
            "service_type": "l2ptp",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
            "live_l2": {
                "endpoints": [
                    {"device": "a", "ac": "Hu0/0/0/1.100", "st": "UP"},
                    {"device": "b", "ac": "Hu0/0/0/2.100", "st": "UP"},
                ],
            },
        },
    }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {"extra": {"services": services}},
        },
    )
    init_service_coverage(case)
    picked = select_dataplane_candidates(case)
    names = {r.get("name") for _e, r in picked}
    soft = names & {"l2-sts-soft-1", "l2-sts-soft-2"}
    assert len(soft) == 1  # one soft-error fills the l2sts slot
    assert "l2-sts-ok" not in names  # soft preferred over healthy peer
    assert "l3-a" in names and "l2-ptp-ok" in names
    assert len(names) == 3
    apply_coverage_after_candidate_select(case, picked)
    peer = ({"l2-sts-soft-1", "l2-sts-soft-2"} - soft).pop()
    assert case.service_coverage[peer] == "category_peer_skipped"
    assert case.service_coverage["l2-sts-ok"] == "category_peer_skipped"


def test_select_dataplane_prefers_live_l2_soft_errors_over_l3rt():
    """Soft-error L2 preferred for its typed slot; other types still get one."""
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    services = {
        "l3rt/x": {
            "name": "l3-a",
            "service_type": "l3rt",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
        },
        "l3rt/y": {
            "name": "l3-b",
            "service_type": "l3rt",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
        },
        "l2sts/z": {
            "name": "l2-sts",
            "service_type": "l2sts",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "status": "up",
            "live_l2": {
                "summary": "unknown",
                "endpoints": [
                    {"error": "ac_not_found", "ac": "Hu0/0/0/4.100"},
                    {"error": "ac_not_found", "ac": "TF0/0/0/23/1.100"},
                ],
            },
        },
    }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {"extra": {"services": services}},
        },
    )
    picked = select_dataplane_candidates(case)
    names = [r.get("name") for _e, r in picked]
    assert "l2-sts" in names
    assert names.count("l2-sts") == 1
    assert sum(1 for n in names if str(n).startswith("l3-")) == 1


def test_parse_dataplane_conclusion():
    got = parse_dataplane_conclusion(
        {
            "dataplane_status": "down",
            "observed": "AC missing from xconnect",
            "cause": "not bound",
        }
    )
    assert got is not None
    assert got["dataplane_status"] == "down"


def test_fallback_ac_not_found_is_unknown():
    """Soft-error live_l2 is insufficient evidence → unknown, not down."""
    assert (
        fallback_dataplane_status(
            {
                "live_l2": {
                    "endpoints": [
                        {"device": "a", "ac": "Hu0/0/0/4.100", "error": "ac_not_found"},
                        {
                            "device": "b",
                            "ac": "TF0/0/0/23/1.100",
                            "error": "ac_not_found",
                        },
                    ]
                }
            }
        )
        == "unknown"
    )


def test_incomplete_verify_is_unknown_even_with_soft_errors():
    from diagnostic_mas.dataplane_verify import _incomplete_verify_finding

    finding = _incomplete_verify_finding(
        {
            "live_l2": {
                "endpoints": [
                    {"device": "a", "ac": "Hu0/0/0/4.100", "error": "ac_not_found"},
                    {"device": "b", "ac": "TF0/0/0/23/1.100", "error": "ac_not_found"},
                ]
            }
        },
        stop_reason="LLM request failed: TimeoutError",
    )
    assert finding["dataplane_status"] == "unknown"
    assert finding["confidence"] == "low"
    assert finding["cause"] == (
        "Dataplane verification incomplete because the LLM request timed out. "
        "Service forwarding status remains unknown."
    )
    assert finding["observed"] == "LLM did not conclude (chat request timed out)"
    assert "heuristic" not in finding["cause"]
    assert "ac_not_found" not in finding["cause"]
    assert "timeout or dataplane budget exhausted" not in finding["observed"]


def test_incomplete_verify_no_tools_not_labeled_timeout():
    from diagnostic_mas.dataplane_verify import _incomplete_verify_finding
    from diagnostic_mas.operator_report import (
        _incomplete_dig_followup,
        _incomplete_dig_next_action,
        _incomplete_dig_stop_kind,
    )

    finding = _incomplete_verify_finding(
        {},
        stop_reason="LLM returned no tools / no conclusion",
    )
    assert "neither tool calls nor a conclude_dataplane" in finding["cause"]
    assert "timed out" not in finding["cause"]
    assert "returned neither tools nor conclude_dataplane" in finding["observed"]
    assert "timeout" not in finding["observed"].lower()
    assert "budget exhausted" not in finding["observed"].lower()

    dx = {
        "observed": finding["observed"],
        "cause": finding["cause"],
        "complete": False,
    }
    assert _incomplete_dig_stop_kind(dx) == "no_conclusion"
    next_action = _incomplete_dig_next_action(dx)
    assert "neither tools nor conclude_dataplane" in next_action
    assert "Fabric timeout" in next_action or "not a Fabric timeout" in next_action
    assert "raise --max-dataplane-tools for this alone" in next_action
    follow = _incomplete_dig_followup("svc-1", dx)
    assert "conclude_dataplane" in follow
    assert "without a conclusion" in follow or "without conclude_dataplane" in follow
    assert "timeout" in follow.lower()  # "not a timeout…"
    assert "not a timeout" in follow.lower()


def test_accept_up_rejected_on_soft_live_l2():
    from diagnostic_mas.dataplane_verify import accept_dataplane_conclusion

    finding = accept_dataplane_conclusion(
        {
            "service_type": "l2ptp",
            "name": "svc1",
            "devices": ["a", "b"],
            "live_l2": {
                "endpoints": [
                    {"device": "a", "error": "ac_not_found"},
                    {"device": "b", "error": "ac_not_found"},
                ]
            },
        },
        {
            "dataplane_status": "up",
            "observed": "other xc UP",
            "cause": "looks fine",
            "confidence": "high",
        },
        session_evidence=[],
    )
    assert finding["dataplane_status"] == "unknown"
    assert "[gate]" in finding["cause"]
    assert "ac_not_found" in finding["cause"]


def test_accept_l2sts_up_ignores_xconnect_ac_not_found():
    """l2sts ACs are in bridge-domain; xconnect ac_not_found must not force down."""
    from diagnostic_mas.dataplane_verify import accept_dataplane_conclusion

    cfg = (
        "evpn\n evi 9001\n  route-target import 398900:9001\n"
        "  route-target export 398900:9001\n"
    )
    session = [
        {
            "kind": "drill",
            "id": "ev_1",
            "payload": {
                "check": "get_device_config",
                "args": {"device": "lbnl-data-sw"},
                "result": cfg,
            },
        },
        {
            "kind": "drill",
            "id": "ev_2",
            "payload": {
                "check": "get_device_config",
                "args": {"device": "renc-data-sw"},
                "result": cfg,
            },
        },
    ]
    finding = accept_dataplane_conclusion(
        {
            "service_type": "l2sts",
            "name": "svc1",
            "devices": ["lbnl-data-sw", "renc-data-sw"],
            "live_l2": {
                "endpoints": [
                    {"device": "lbnl-data-sw", "error": "ac_not_found"},
                    {"device": "renc-data-sw", "error": "ac_not_found"},
                ]
            },
        },
        {
            "dataplane_status": "up",
            "observed": "BD up, RTs match",
            "cause": "path ok",
            "confidence": "high",
        },
        session_evidence=session,
    )
    assert finding["dataplane_status"] == "up"


def test_accept_up_rejected_without_endpoint_coverage():
    from diagnostic_mas.dataplane_verify import accept_dataplane_conclusion

    finding = accept_dataplane_conclusion(
        {
            "service_type": "l2ptp",
            "name": "svc1",
            "devices": ["lbnl-data-sw", "renc-data-sw"],
        },
        {
            "dataplane_status": "up",
            "observed": "xc up",
            "cause": "ok",
            "confidence": "high",
        },
        session_evidence=[],
    )
    assert finding["dataplane_status"] == "unknown"
    assert "not checked" in finding["cause"]
    assert finding["complete"] is False


def test_commit_gate_unknown_does_not_explain_issue():
    from diagnostic_mas.case import open_issue
    from diagnostic_mas.dataplane_verify import (
        _commit_dataplane_conclusion,
        accept_dataplane_conclusion,
        dataplane_diagnosed_names,
    )

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    record = {
        "service_type": "l2sts",
        "name": "svc1",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "devices": ["lbnl-data-sw", "renc-data-sw"],
    }
    iid = open_issue(
        case,
        code="service_unknown",
        message="svc1",
        evidence_ids=[],
        layer="services",
        edge_id="svc1",
    )
    finding = accept_dataplane_conclusion(
        record,
        {
            "dataplane_status": "up",
            "observed": "claimed up",
            "cause": "ok",
            "confidence": "high",
        },
        session_evidence=[],
    )
    _commit_dataplane_conclusion(
        case, record, finding, source="llm", evidence_ids=[]
    )
    assert record["dataplane_status"] == "unknown"
    assert case.issues[0]["id"] == iid
    assert case.issues[0]["status"] == "open"
    assert case.diagnoses[0]["complete"] is False
    assert "svc1" not in dataplane_diagnosed_names(case)
    assert any(e.get("kind") == "dataplane_incomplete" for e in case.evidence)


def test_commit_llm_unknown_does_not_explain_issue():
    """Direct LLM unknown is unresolved — not explained / not diagnosed-done."""
    from diagnostic_mas.case import open_issue
    from diagnostic_mas.dataplane_verify import (
        _commit_dataplane_conclusion,
        dataplane_diagnosed_names,
    )

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    record = {
        "service_type": "l2sts",
        "name": "svc1",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "devices": ["lbnl-data-sw", "renc-data-sw"],
    }
    open_issue(
        case,
        code="service_unknown",
        message="svc1",
        evidence_ids=[],
        layer="services",
        edge_id="svc1",
    )
    _commit_dataplane_conclusion(
        case,
        record,
        {
            "dataplane_status": "unknown",
            "observed": "could not confirm BD state",
            "cause": "insufficient evidence on one PE",
            "confidence": "low",
        },
        source="llm",
        evidence_ids=["ev_1"],
    )
    assert record["dataplane_status"] == "unknown"
    assert case.issues[0]["status"] == "open"
    assert case.diagnoses[0]["complete"] is False
    assert case.diagnoses[0]["source"] == "llm"
    assert "svc1" not in dataplane_diagnosed_names(case)
    assert any(e.get("kind") == "dataplane_incomplete" for e in case.evidence)


def test_accept_l2sts_up_requires_both_pe_config_not_rt_match():
    """l2sts up needs config-like evidence on both PEs; RT match is dig-only."""
    from diagnostic_mas.dataplane_verify import accept_dataplane_conclusion

    cfg_a = "evpn\n evi 9001\n  route-target import 1:1\n  route-target export 1:1\n"
    cfg_b = "evpn\n evi 9001\n  route-target import 2:2\n  route-target export 2:2\n"
    session = [
        {
            "kind": "drill",
            "id": "ev_1",
            "payload": {
                "check": "get_device_config",
                "args": {"device": "lbnl-data-sw"},
                "result": cfg_a,
            },
        },
        {
            "kind": "drill",
            "id": "ev_2",
            "payload": {
                "check": "get_device_config",
                "args": {"device": "renc-data-sw"},
                "result": cfg_b,
            },
        },
    ]
    # Divergent RTs are no longer auto-demoted by the soft gate.
    finding_mismatch = accept_dataplane_conclusion(
        {
            "service_type": "l2sts",
            "name": "svc1",
            "devices": ["lbnl-data-sw", "renc-data-sw"],
        },
        {
            "dataplane_status": "up",
            "observed": "RTs look fine",
            "cause": "match",
            "confidence": "high",
        },
        session_evidence=session,
    )
    assert finding_mismatch["dataplane_status"] == "up"

    # Missing config-like evidence on one PE still demotes (both devices present).
    session_no_cfg = [
        session[0],
        {
            "kind": "drill",
            "id": "ev_2",
            "payload": {
                "check": "get_interface_health",
                "args": {"device": "renc-data-sw"},
                "result": "ok",
            },
        },
    ]
    finding_missing = accept_dataplane_conclusion(
        {
            "service_type": "l2sts",
            "name": "svc1",
            "devices": ["lbnl-data-sw", "renc-data-sw"],
        },
        {
            "dataplane_status": "up",
            "observed": "one PE config only",
            "cause": "incomplete",
            "confidence": "high",
        },
        session_evidence=session_no_cfg,
    )
    assert finding_missing["dataplane_status"] == "unknown"
    assert "both PEs" in finding_missing["cause"]

    session[1]["payload"]["result"] = cfg_a
    finding_ok = accept_dataplane_conclusion(
        {
            "service_type": "l2sts",
            "name": "svc1",
            "devices": ["lbnl-data-sw", "renc-data-sw"],
        },
        {
            "dataplane_status": "up",
            "observed": "RTs match",
            "cause": "match",
            "confidence": "high",
        },
        session_evidence=session,
    )
    assert finding_ok["dataplane_status"] == "up"


def test_scrub_l2sts_pseudoport_flood_overclaim():
    """nso18: pseudo-port up ≠ expected remote peer in flood list."""
    from diagnostic_mas.dataplane_verify import (
        _scrub_l2sts_pseudoport_flood_overclaim,
        accept_dataplane_conclusion,
    )

    nso18_obs = (
        "BD-scoped install: each PE's l2vpn forwarding BD detail shows "
        "2 bridge ports = local AC (oper up) + EVPN pseudo-port state Up "
        "for evi 9011, i.e. EVPN flood state present in THIS BD on both "
        "sides."
    )
    scrubbed = _scrub_l2sts_pseudoport_flood_overclaim(
        {
            "dataplane_status": "up",
            "observed": nso18_obs,
            "cause": (
                "PE-side readiness: EVI 9011 EVPN pseudo-port installed in "
                "each BD's forwarding table."
            ),
            "confidence": "high",
        }
    )
    blob = f"{scrubbed['observed']}\n{scrubbed['cause']}".lower()
    assert "does not by itself prove" in blob
    assert "flood list" in blob
    assert "missing check" in blob
    assert "i.e. evpn flood state present" not in blob
    assert scrubbed["dataplane_status"] == "up"

    careful = (
        "EVPN pseudo-port up on both PEs; does not by itself prove the "
        "expected remote peer is installed in the flood list (missing "
        "check: peer-specific replication / flood-list state)."
    )
    kept = _scrub_l2sts_pseudoport_flood_overclaim(
        {"dataplane_status": "up", "observed": careful, "cause": ""}
    )
    assert kept["observed"] == careful

    session = [
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "wash-data-sw",
                    "input_command": "l2vpn forwarding bridge-domain",
                },
                "result": "bridge-domain up AC up EVPN pseudo-port Up",
            },
        },
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "max-data-sw",
                    "input_command": "l2vpn forwarding bridge-domain",
                },
                "result": "bridge-domain up AC up EVPN pseudo-port Up",
            },
        },
    ]
    finding = accept_dataplane_conclusion(
        {
            "service_type": "l2sts",
            "name": "L2STS_MAX_WASH",
            "devices": ["wash-data-sw", "max-data-sw"],
        },
        {
            "dataplane_status": "up",
            "observed": nso18_obs,
            "cause": "ACs up, BD up, EVPN pseudo-port up both sides.",
            "confidence": "high",
        },
        session_evidence=session,
    )
    # Status may demote for incomplete bidirectional proof; scrub must apply.
    assert "does not by itself prove" in finding["observed"].lower()
    assert "i.e. evpn flood state present" not in finding["observed"].lower()


def test_scrub_l2ptp_invented_counter_clear_hypothesis():
    """nso17 L2PTP: asymmetric counters ≠ proven different clear times."""
    from diagnostic_mas.dataplane_verify import (
        _scrub_unverified_counter_hypotheses,
        accept_dataplane_conclusion,
    )

    nso17_observed = (
        "Non-zero forwarding counters in both directions (salt AC sent 293 "
        "from EVPN; atla AC sent 6.5G from EVPN), though per-end totals "
        "differ, likely due to different counter-clear times; no "
        "customer-endpoint delivery test performed."
    )
    scrubbed = _scrub_unverified_counter_hypotheses(
        {
            "dataplane_status": "up",
            "observed": nso17_observed,
            "cause": "PE-side XC up both ends.",
            "confidence": "high",
        }
    )
    blob = f"{scrubbed['observed']}\n{scrubbed['cause']}".lower()
    assert "reason was not established" in blob
    assert "missing check" in blob and "counter-clear" in blob
    assert "likely due to different counter-clear" not in blob
    assert scrubbed["dataplane_status"] == "up"

    # Accurate wording must pass through unchanged.
    accurate = (
        "Packet-counter totals differ; the reason was not established. "
        "Missing check: counter-clear times on both ACs."
    )
    kept = _scrub_unverified_counter_hypotheses(
        {"dataplane_status": "up", "observed": accurate, "cause": ""}
    )
    assert kept["observed"] == accurate

    session = [
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "salt-data-sw",
                    "input_command": "l2vpn xconnect",
                },
                "result": "xconnect group up AC up",
            },
        },
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "atla-data-sw",
                    "input_command": "l2vpn xconnect",
                },
                "result": "xconnect group up AC up",
            },
        },
    ]
    finding = accept_dataplane_conclusion(
        {
            "service_type": "l2ptp",
            "name": "P4_SALT_ATLA",
            "devices": ["salt-data-sw", "atla-data-sw"],
            "live_l2": {
                "salt-data-sw": {"status": "up"},
                "atla-data-sw": {"status": "up"},
            },
        },
        {
            "dataplane_status": "up",
            "observed": nso17_observed,
            "cause": "Both ACs and XC segments up.",
            "confidence": "high",
        },
        session_evidence=session,
    )
    assert finding["dataplane_status"] == "up"
    assert "reason was not established" in finding["observed"].lower()
    assert "likely due to different counter-clear" not in finding["observed"].lower()


def test_accept_l2sts_up_demotes_import_ruled_out_from_no_local_macs():
    """nso17: no local MACs on peer ≠ proof far-end import would succeed."""
    from diagnostic_mas.dataplane_verify import (
        _l2sts_up_admits_incomplete_bidirectional_proof,
        accept_dataplane_conclusion,
    )

    nso17_cause = (
        "No fault: PE-side forwarding readiness is established — both ACs "
        "up and bound to the EVI 9037 bridge-domain on each PE, EVPN state "
        "up, and effective RT 398900:9037 matches in both directions with "
        "utah's MAC routes actually imported into kans's BD 204. The "
        "absence of remote MACs at utah is explained by verified evidence "
        "that kans's BD has no locally learned MACs to advertise, not by "
        "an RT or import failure. Remaining uncertainty: kans→utah proof "
        "rests on the IMET route with matching RT rather than an observed "
        "BD-level install, and end-to-end traffic delivery was not tested."
    )
    assert _l2sts_up_admits_incomplete_bidirectional_proof(nso17_cause)

    session = [
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "utah-data-sw",
                    "input_command": "l2vpn bridge-domain detail",
                },
                "result": "bridge-domain up AC up",
            },
        },
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "kans-data-sw",
                    "input_command": "l2vpn bridge-domain detail",
                },
                "result": "bridge-domain up AC up remote mac",
            },
        },
    ]
    finding = accept_dataplane_conclusion(
        {
            "service_type": "l2sts",
            "name": "P4_KANS_NET",
            "devices": ["utah-data-sw", "kans-data-sw"],
        },
        {
            "dataplane_status": "up",
            "observed": (
                "utah→kans BD install confirmed; kans→utah IMET/RT only; "
                "utah shows no remote MACs; kans has no locally learned MACs."
            ),
            "cause": nso17_cause,
            "confidence": "high",
        },
        session_evidence=session,
    )
    assert finding["dataplane_status"] == "unknown"
    assert "import" in finding["cause"].lower()

    # Allowed cautious framing must not trip the gate.
    ok_cause = (
        "No forwarding fault was demonstrated by the checks performed. "
        "UTAH→KANS MAC distribution is confirmed. KANS→UTAH has supporting "
        "IMET/RT evidence, but service-level forwarding installation and "
        "customer traffic delivery remain unverified."
    )
    assert _l2sts_up_admits_incomplete_bidirectional_proof(ok_cause) is None


def test_accept_l2sts_up_keeps_bidirectional_bd_install_rt_could_not_be_read():
    """nso23 wording: numeric RT could not be read / not obtainable."""
    from diagnostic_mas.dataplane_verify import (
        _l2sts_up_admits_incomplete_bidirectional_proof,
        accept_dataplane_conclusion,
    )

    observed = (
        "Identity: gpn/utah ACs up in BD; Numeric EVI/RT text was not "
        "obtainable (see Unverified).\n"
        "Route exchange both ways: each PE's locally learned MAC installed "
        "as EVPN in the opposite BD."
    )
    cause = (
        "No fault: ACs up, BD up on both PEs, and bidirectional EVPN MAC "
        "distribution with each PE's locally learned MAC installed as an "
        "EVPN entry in the opposite PE's BD. That two-way BD-scoped install "
        "demonstrates effective RT import/export compatibility for this EVI "
        "even though numeric RT values could not be read. Passed PE-side "
        "readiness checks; customer traffic delivery was not tested."
    )
    assert (
        _l2sts_up_admits_incomplete_bidirectional_proof(f"{observed}\n{cause}")
        is None
    )
    session = [
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "gpn-data-sw",
                    "input_command": "l2vpn bridge-domain detail",
                },
                "result": "bd up EVPN mac",
            },
        },
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "utah-data-sw",
                    "input_command": "l2vpn bridge-domain detail",
                },
                "result": "bd up EVPN mac",
            },
        },
    ]
    finding = accept_dataplane_conclusion(
        {
            "service_type": "l2sts",
            "name": "l2_ctrl_ue_upf-99f1a75f",
            "devices": ["gpn-data-sw", "utah-data-sw"],
        },
        {
            "dataplane_status": "up",
            "observed": observed,
            "cause": cause,
            "confidence": "medium",
        },
        session_evidence=session,
    )
    assert finding["dataplane_status"] == "up"


def test_accept_l2sts_up_demotes_imet_only_empty_mac_both_ways():
    """nso23: IMET-only + empty MAC both PEs must not stay up (uniform bar)."""
    from diagnostic_mas.dataplane_verify import (
        _l2sts_up_admits_incomplete_bidirectional_proof,
        accept_dataplane_conclusion,
    )

    fabric_obs = (
        "Unverified: no MAC entries for either AC in l2vpn forwarding "
        "(both ACs show 0 packets received), so no type-2 MAC distribution "
        "was observed in either direction; BD flood-list/replication "
        "membership not checked."
    )
    fabric_cause = (
        "No fault identified: both ACs up and this EVI's RD table on each "
        "PE contains the opposite PE's IMET route as best path, establishing "
        "bidirectional EVPN route exchange and effective RT compatibility. "
        "Passed PE-side readiness checks; customer traffic delivery was not "
        "tested — the absence of MAC entries coincides with zero received "
        "packets on both ACs."
    )
    assert _l2sts_up_admits_incomplete_bidirectional_proof(
        f"{fabric_obs}\n{fabric_cause}"
    )

    ue_obs = (
        "Unverified: 0 MAC addresses (SW/HW) in this BD on both PEs, so no "
        "type-2 MAC distribution was demonstrated in either direction; "
        "peer-specific flood/replication-list membership was not shown."
    )
    ue_cause = (
        "No forwarding fault was demonstrated: both ACs and EVI-9032 EVPN "
        "pseudo-port are up on both PEs, and each PE has the peer's IMET "
        "route for this EVI's RD, giving bidirectional service-scoped "
        "route/RT evidence. Passed PE-side readiness checks. Residual "
        "uncertainty: MAC tables for this BD are empty on both PEs."
    )
    assert _l2sts_up_admits_incomplete_bidirectional_proof(f"{ue_obs}\n{ue_cause}")

    session = [
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "newy-data-sw",
                    "input_command": "l2vpn bridge-domain detail",
                },
                "result": "bd up",
            },
        },
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "losa-data-sw",
                    "input_command": "l2vpn bridge-domain detail",
                },
                "result": "bd up",
            },
        },
    ]
    finding = accept_dataplane_conclusion(
        {
            "service_type": "l2sts",
            "name": "fabric_network-5194f01c",
            "devices": ["newy-data-sw", "losa-data-sw"],
        },
        {
            "dataplane_status": "up",
            "observed": fabric_obs,
            "cause": fabric_cause,
            "confidence": "medium",
        },
        session_evidence=session,
    )
    assert finding["dataplane_status"] == "unknown"
    assert "no type-2" in finding["cause"].lower() or "mac install" in finding["cause"].lower()


def test_accept_l2sts_up_keeps_bidirectional_bd_install_without_numeric_rt():
    """nso22: numeric RT not retrieved ≠ missing bidirectional BD proof."""
    from diagnostic_mas.dataplane_verify import (
        _l2sts_up_admits_incomplete_bidirectional_proof,
        accept_dataplane_conclusion,
    )

    observed = (
        "- Identity: gpn AC up, utah AC up, EVI 9027 both PEs.\n"
        "- Route exchange gpn→utah: utah BD holds MAC f61e as type EVPN — "
        "same MAC learned on gpn AC. Remote install into THIS BD proves "
        "export from gpn and RT import into utah EVI 9027.\n"
        "- Route exchange utah→gpn: gpn BD holds MAC 9660 as type EVPN — "
        "same MAC learned on utah AC. Confirms the reverse direction; "
        "effective RT compatibility established both ways by BD-scoped "
        "install (numeric RT text not needed/not retrieved).\n"
        "- Unverified: no customer traffic delivery test performed; "
        "numeric/effective RT values not quoted (BD-scoped bidirectional "
        "EVPN install used instead); per-peer flood/replication list "
        "membership not separately checked."
    )
    cause = (
        "No fault — PE-side forwarding readiness is positively established "
        "on both endpoints: ACs up, bridge-domains up under EVI 9027, and "
        "each PE's bridge-domain has installed the peer's locally learned "
        "MAC as an EVPN entry, demonstrating EVPN route exchange and "
        "effective RT import/export in both directions. Passed PE-side "
        "readiness checks; customer traffic delivery was not tested."
    )
    assert (
        _l2sts_up_admits_incomplete_bidirectional_proof(f"{observed}\n{cause}")
        is None
    )

    session = [
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "gpn-data-sw",
                    "input_command": "l2vpn bridge-domain detail",
                },
                "result": "bridge-domain up AC up EVPN mac",
            },
        },
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "utah-data-sw",
                    "input_command": "l2vpn bridge-domain detail",
                },
                "result": "bridge-domain up AC up EVPN mac",
            },
        },
    ]
    finding = accept_dataplane_conclusion(
        {
            "service_type": "l2sts",
            "name": "l2_ctrl_ue_upf-99f1a75f",
            "devices": ["gpn-data-sw", "utah-data-sw"],
        },
        {
            "dataplane_status": "up",
            "observed": observed,
            "cause": cause,
            "confidence": "medium",
        },
        session_evidence=session,
    )
    assert finding["dataplane_status"] == "up"
    assert "[gate]" not in str(finding.get("cause") or "")


def test_accept_l2sts_up_demotes_partial_bidirectional_admission():
    """Gemma-style partial MAC proof must not stay dataplane=up."""
    from diagnostic_mas.dataplane_verify import (
        _l2sts_up_admits_incomplete_bidirectional_proof,
        accept_dataplane_conclusion,
    )

    gemma_cause = (
        "Forwarding readiness is partially verified: ACs and Bridge-Domains "
        "are UP on both PEs, and BGP L2VPN EVPN sessions are established. "
        "However, bidirectional MAC learning could not be fully confirmed as "
        "the MAC table on utah-data-sw was not retrievable, while kans-data-sw "
        "shows remote MACs (EVPN) for BD 204. Traffic delivery is unverified."
    )
    assert _l2sts_up_admits_incomplete_bidirectional_proof(gemma_cause)

    # Enough both-PE session evidence to pass the configish gate so the
    # bidirectional-admission gate is what demotes.
    session = [
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "utah-data-sw",
                    "input_command": "l2vpn bridge-domain detail",
                },
                "result": "bridge-domain up AC up",
            },
        },
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "kans-data-sw",
                    "input_command": "l2vpn bridge-domain detail",
                },
                "result": "bridge-domain up AC up remote mac",
            },
        },
    ]
    finding = accept_dataplane_conclusion(
        {
            "service_type": "l2sts",
            "name": "P4_KANS_NET",
            "devices": ["utah-data-sw", "kans-data-sw"],
        },
        {
            "dataplane_status": "up",
            "observed": (
                "kans MAC table shows remote EVPN; utah MAC table could not "
                "be retrieved."
            ),
            "cause": gemma_cause,
            "confidence": "medium",
        },
        session_evidence=session,
    )
    assert finding["dataplane_status"] == "unknown"
    assert finding.get("complete") is False
    assert "[gate]" in finding["cause"]
    assert "partial forwarding verification" in finding["cause"].lower()
    assert "config-like evidence on both PEs" not in finding["cause"]

    assert (
        _l2sts_up_admits_incomplete_bidirectional_proof(
            "Both ACs/BDs up; remote MACs learned both directions; "
            "traffic delivery unverified."
        )
        is None
    )


def test_accept_l2sts_up_demotes_counter_as_reverse_proof():
    """Near-matching AC counters are not kans→utah EVPN/RT proof."""
    from diagnostic_mas.dataplane_verify import (
        _l2sts_up_admits_incomplete_bidirectional_proof,
        accept_dataplane_conclusion,
    )

    opus_cause = (
        "No fault: PE-side forwarding readiness is positive on both endpoints "
        "(ACs up, BD/EVI 9037 up EVPN-native, EVPN BGP sessions established), "
        "and service-scoped forwarding evidence exists in both directions — "
        "utah's local MACs installed as EVPN entries in kans BD 204 (utah→kans) "
        "and kans AC ingress matched by utah AC egress in this BD (kans→utah), "
        "which operationally demonstrates effective RT import/export "
        "compatibility for this EVI without numeric RTs being retrievable. "
        "Utah's BD shows no remote EVPN MACs currently, consistent with the "
        "very low kans-side customer ingress (99 frames) and 300 s inactivity "
        "aging, not with a distribution failure."
    )
    opus_observed = (
        "utah→kans distribution proven via remote EVPN MACs on kans. "
        "kans→utah delivery evidence: kans AC received 99 pkts; utah AC "
        "sent 98 pkts — frames crossed the EVPN core."
    )
    assert _l2sts_up_admits_incomplete_bidirectional_proof(
        f"{opus_observed}\n{opus_cause}"
    )

    session = [
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "utah-data-sw",
                    "input_command": "l2vpn bridge-domain detail",
                },
                "result": "bd up",
            },
        },
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device_name": "kans-data-sw",
                    "input_command": "l2vpn bridge-domain detail",
                },
                "result": "bd up remote mac",
            },
        },
    ]
    finding = accept_dataplane_conclusion(
        {
            "service_type": "l2sts",
            "name": "P4_KANS_NET",
            "devices": ["utah-data-sw", "kans-data-sw"],
        },
        {
            "dataplane_status": "up",
            "observed": opus_observed,
            "cause": opus_cause,
            "confidence": "high",
        },
        session_evidence=session,
    )
    assert finding["dataplane_status"] == "unknown"
    assert finding.get("complete") is False
    assert "packet counters" in finding["cause"].lower() or "counters" in finding[
        "cause"
    ].lower()


def test_accept_non_up_passes_through():
    from diagnostic_mas.dataplane_verify import accept_dataplane_conclusion

    finding = {
        "dataplane_status": "down",
        "observed": "AC missing",
        "cause": "not in xconnect",
        "confidence": "high",
    }
    out = accept_dataplane_conclusion(
        {
            "live_l2": {
                "endpoints": [
                    {"error": "ac_not_found"},
                    {"error": "ac_not_found"},
                ]
            }
        },
        finding,
        session_evidence=[],
    )
    assert out["dataplane_status"] == "down"
    assert out["cause"] == "not in xconnect"


def test_necessary_check_failed_not_cleared_by_unrelated_show():
    """Failed xconnect show must not be cleared by a successful version show."""
    from diagnostic_mas.dataplane_verify import _necessary_check_failed

    session = [
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device": "lbnl-data-sw",
                    "input_command": "l2vpn xconnect",
                },
                "error": "NED timeout",
            },
        },
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device": "lbnl-data-sw",
                    "input_command": "version",
                },
                "result": "Cisco IOS XR Software",
            },
        },
    ]
    msg = _necessary_check_failed(session, ["lbnl-data-sw", "renc-data-sw"])
    assert msg is not None
    assert "xconnect" in msg
    assert "NED timeout" in msg


def test_necessary_check_failed_cleared_by_same_show_retry():
    from diagnostic_mas.dataplane_verify import _necessary_check_failed

    session = [
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device": "lbnl-data-sw",
                    "input_command": "l2vpn xconnect",
                },
                "error": "NED timeout",
            },
        },
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device": "lbnl-data-sw",
                    "input_command": "show l2vpn xconnect",
                },
                "result": "Group  Name",
            },
        },
    ]
    assert _necessary_check_failed(session, ["lbnl-data-sw"]) is None


def test_accept_up_demoted_when_required_show_still_failed():
    from diagnostic_mas.dataplane_verify import accept_dataplane_conclusion

    cfg = (
        "evpn\n evi 9001\n  route-target import 1:1\n"
        "  route-target export 1:1\n"
    )
    session = [
        {
            "kind": "drill",
            "payload": {
                "check": "get_device_config",
                "args": {"device": "lbnl-data-sw"},
                "result": cfg,
            },
        },
        {
            "kind": "drill",
            "payload": {
                "check": "get_device_config",
                "args": {"device": "renc-data-sw"},
                "result": cfg,
            },
        },
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device": "lbnl-data-sw",
                    "input_command": "l2vpn xconnect",
                },
                "error": "timeout",
            },
        },
        {
            "kind": "drill",
            "payload": {
                "check": "exec_show",
                "args": {
                    "device": "lbnl-data-sw",
                    "input_command": "version",
                },
                "result": "XR",
            },
        },
    ]
    finding = accept_dataplane_conclusion(
        {
            "service_type": "l2sts",
            "name": "svc1",
            "devices": ["lbnl-data-sw", "renc-data-sw"],
        },
        {
            "dataplane_status": "up",
            "observed": "looks fine",
            "cause": "ok",
            "confidence": "high",
        },
        session_evidence=session,
    )
    assert finding["dataplane_status"] == "unknown"
    assert "xconnect" in finding["cause"]
    assert "[gate]" in finding["cause"]


def test_fallback_without_live_l2_is_unknown():
    assert fallback_dataplane_status({"name": "x"}) == "unknown"


def test_dataplane_tools_cap_preserves_zero():
    from diagnostic_mas.dataplane_verify import dataplane_tools_cap

    assert dataplane_tools_cap(Budget(max_deep_checks=0, max_handoffs=0)) == 40
    b0 = Budget(max_deep_checks=0, max_handoffs=0, max_dataplane_tools=0)
    assert dataplane_tools_cap(b0) == 0
    b5 = Budget(max_deep_checks=0, max_handoffs=0, max_dataplane_tools=5)
    assert dataplane_tools_cap(b5) == 5


@pytest.mark.asyncio
async def test_budget_exhaustion_still_allows_conclude_round(monkeypatch):
    """Last mcp_call may spend the final tool credit; LLM still gets a conclude turn."""
    from diagnostic_mas import dataplane_verify as dv

    class _Fn:
        def __init__(self, name, arguments, id_="1"):
            self.name = name
            self.arguments = arguments
            self.id = id_

    class _Call:
        def __init__(self, fn):
            self.function = fn
            self.id = fn.id

    class _Msg:
        def __init__(self, tool_calls=None, content=None):
            self.tool_calls = tool_calls
            self.content = content

    class _Choice:
        def __init__(self, message):
            self.message = message

    class _Resp:
        def __init__(self, message):
            self.choices = [_Choice(message)]

    class FakeOAI:
        def __init__(self):
            self.chat = self
            self.completions = self
            self.n = 0
            self.rounds = []

        def create(self, **kwargs):
            self.n += 1
            self.rounds.append(self.n)
            if self.n == 1:
                return _Resp(
                    _Msg(
                        tool_calls=[
                            _Call(
                                _Fn(
                                    "mcp_call",
                                    json.dumps(
                                        {
                                            "tool_name": "get_interface_health",
                                            "params": {"device": "lbnl-data-sw"},
                                        }
                                    ),
                                    id_="t1",
                                )
                            )
                        ]
                    )
                )
            return _Resp(
                _Msg(
                    tool_calls=[
                        _Call(
                            _Fn(
                                "conclude_dataplane",
                                (
                                    '{"dataplane_status":"down",'
                                    '"observed":"used last tool result",'
                                    '"cause":"AC down from final probe"}'
                                ),
                                id_="c1",
                            )
                        )
                    ]
                )
            )

    async def fake_exec(client, case, **kwargs):
        return "interface down"

    monkeypatch.setattr(dv, "execute_one_drill_call", fake_exec)

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    rec = {
        "name": "svc1",
        "service_type": "l2ptp",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "devices": ["lbnl-data-sw"],
    }
    session = DrillSession(max_tools=1)

    class S:
        fabric_model = "m"
        fabric_api_key = "k"
        fabric_api_url = "http://x"

    oai = FakeOAI()
    ok = await dv.llm_dataplane_verify_one(
        object(),
        S(),
        case,
        record=rec,
        device_names={"lbnl-data-sw"},
        session=session,
        openai_client=oai,
    )
    assert ok is True
    assert oai.n >= 2  # probe round + conclude round after budget==0
    assert rec["dataplane_status"] == "down"
    assert case.diagnoses[0]["complete"] is True
    assert "final probe" in case.diagnoses[0]["cause"] or "AC down" in case.diagnoses[0]["cause"]


@pytest.mark.asyncio
async def test_zero_dataplane_tools_skips_verify():
    from diagnostic_mas.dataplane_verify import run_dataplane_verify_phase

    case = CaseFile(
        budget=Budget(
            max_deep_checks=0,
            max_handoffs=0,
            max_drill_issues=2,
            max_dataplane_tools=0,
        )
    )
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "extra": {
                    "services": {
                        "l2sts/svc1": {
                            "name": "svc1",
                            "service_type": "l2sts",
                            "system_status": "up",
                            "dataplane_status": "not_checked",
                        }
                    }
                }
            },
        },
    )

    class S:
        fabric_model = "m"
        fabric_api_key = "k"
        fabric_api_url = "http://x"

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("LLM must not be called when tools_cap=0")

    await run_dataplane_verify_phase(
        object(),
        S(),
        case,
        device_names=set(),
        openai_client=Boom(),
    )
    assert case.diagnoses == []
    assert not any(
        e.get("kind") in {"dataplane_finding", "dataplane_incomplete"}
        for e in case.evidence
    )


def test_narrative_from_dataplane_findings():
    from diagnostic_mas.roles.summary import narrative_from_dataplane_findings

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_evidence(
        case,
        {
            "kind": "dataplane_finding",
            "payload": {
                "service_type": "l2sts",
                "name": "svc1",
                "dataplane_status": "down",
                "observed": "AC missing " + ("detail " * 80),
                "cause": "not in xconnect",
                "source": "llm",
            },
        },
    )
    text = narrative_from_dataplane_findings(case)
    assert "dataplane=down" in text
    assert "not in xconnect" in text
    assert "Observed:" not in text
    assert "detail detail" not in text  # full Observed not dumped


def test_narrative_prefers_diagnoses_over_evidence():
    from diagnostic_mas.case import add_diagnosis
    from diagnostic_mas.roles.summary import narrative_from_dataplane_findings

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_evidence(
        case,
        {
            "kind": "dataplane_finding",
            "payload": {
                "service_type": "l2sts",
                "name": "svc1",
                "dataplane_status": "up",
                "observed": "legacy evidence",
                "cause": "should not appear",
                "source": "llm",
            },
        },
    )
    add_diagnosis(
        case,
        kind="dataplane",
        source="llm",
        status="down",
        subject={"service_type": "l2sts", "name": "svc1"},
        observed="AC missing",
        cause="not in xconnect",
    )
    text = narrative_from_dataplane_findings(case)
    assert "dataplane=down" in text
    assert "not in xconnect" in text
    assert "should not appear" not in text


def test_narrative_incomplete_timeout_preserves_cause_not_sync_xconnect():
    from diagnostic_mas.case import add_diagnosis
    from diagnostic_mas.roles.summary import narrative_from_dataplane_findings

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_diagnosis(
        case,
        kind="dataplane",
        source="fallback",
        status="unknown",
        subject={"service_type": "l2sts", "name": "P4_KANS_NET"},
        observed="LLM did not conclude (chat request timed out)",
        cause=(
            "Dataplane verification incomplete because the LLM request "
            "timed out. Service forwarding status remains unknown."
        ),
        extra={"complete": False},
    )
    text = narrative_from_dataplane_findings(case)
    assert "timed out" in text.lower()
    assert "LLM timeout" in text or "dataplane LLM timeout" in text.lower()
    assert "service sync" not in text.lower()
    assert "xconnect" not in text.lower()


def test_narrative_snips_long_cause():
    from diagnostic_mas.case import add_diagnosis
    from diagnostic_mas.roles.summary import narrative_from_dataplane_findings

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    long_cause = "word " * 200
    add_diagnosis(
        case,
        kind="dataplane",
        source="llm",
        status="up",
        subject={"service_type": "l2ptp", "name": "svc1"},
        observed="huge observed " * 100,
        cause=long_cause,
        fix_suggestion="optional follow-up " * 50,
    )
    text = narrative_from_dataplane_findings(case)
    assert "dataplane=up" in text
    assert "Passed PE-side readiness checks" in text
    assert "Customer traffic delivery was not tested" in text
    assert "word word" not in text  # raw dig cause not used for up
    assert "Observed:" not in text
    assert "huge observed" not in text
    assert "…" in text  # long fix_suggestion snipped
    assert len(text) < 800


def test_narrative_up_default_next_when_no_fix():
    from diagnostic_mas.case import add_diagnosis
    from diagnostic_mas.roles.summary import narrative_from_dataplane_findings

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_diagnosis(
        case,
        kind="dataplane",
        source="llm",
        status="up",
        subject={"service_type": "l3rt", "name": "ceph1"},
        observed="BVI up, FIB present",
        cause="likely idle endpoint",
    )
    text = narrative_from_dataplane_findings(case)
    assert "likely idle" not in text
    assert "Passed PE-side readiness checks" in text
    assert "Customer traffic delivery was not tested" in text
    assert "scoped reachability" in text


@pytest.mark.asyncio
async def test_summary_narrative_deterministic_opt_in(capsys):
    from diagnostic_mas.case import add_diagnosis
    from diagnostic_mas.roles.summary import summary_narrative

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_diagnosis(
        case,
        kind="dataplane",
        source="llm",
        status="down",
        subject={"service_type": "l2ptp", "name": "svc1"},
        observed="AC DN",
        cause="attachment down",
    )

    class S:
        fabric_api_key = "k"
        fabric_model = "m"

    def boom(_s, _c):
        raise AssertionError("final summary LLM must not be called")

    text = await summary_narrative(
        case,
        S(),
        skip_llm=False,
        deterministic_summary=True,
        llm_summary_fn=boom,  # type: ignore[arg-type]
    )
    assert "dataplane=down" in text
    assert "attachment down" in text
    assert "deterministic dataplane" in capsys.readouterr().err

    llm_text = await summary_narrative(
        case,
        S(),
        skip_llm=False,
        deterministic_summary=False,
        llm_summary_fn=lambda _s, _c: "LLM Summary prose",  # type: ignore[arg-type]
    )
    assert llm_text == "LLM Summary prose"
    assert "should not appear" not in text


def test_record_dataplane_finding_dual_writes_diagnosis():
    from diagnostic_mas.dataplane_verify import _record_dataplane_finding

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    record = {
        "service_type": "l2sts",
        "name": "svc1",
        "system_status": "up",
        "dataplane_status": "not_checked",
    }
    _record_dataplane_finding(
        case,
        record,
        {
            "dataplane_status": "down",
            "observed": "AC missing",
            "cause": "not in xconnect",
            "fix_suggestion": None,
            "confidence": "high",
        },
        source="llm",
        evidence_ids=["ev_1"],
    )
    findings = [e for e in case.evidence if e.get("kind") == "dataplane_finding"]
    assert len(findings) == 1
    assert len(case.diagnoses) == 1
    dx = case.diagnoses[0]
    assert dx["kind"] == "dataplane"
    assert dx["status"] == "down"
    assert dx["complete"] is True
    assert dx["evidence_ids"] == ["ev_1"]
    assert dx["subject"]["name"] == "svc1"
    assert record["dataplane_status"] == "down"


def test_incomplete_verify_does_not_mark_issue_explained():
    from diagnostic_mas.case import open_issue
    from diagnostic_mas.dataplane_verify import (
        _incomplete_verify_finding,
        _record_incomplete_dataplane_verify,
        dataplane_diagnosed_names,
    )

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    record = {
        "service_type": "l2sts",
        "name": "svc1",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "live_l2": {
            "endpoints": [
                {"error": "ac_not_found"},
                {"error": "ac_not_found"},
            ]
        },
    }
    iid = open_issue(
        case,
        code="service_down",
        message="svc1",
        evidence_ids=[],
        layer="services",
        edge_id="svc1",
    )
    _record_incomplete_dataplane_verify(
        case, record, _incomplete_verify_finding(record)
    )
    assert record["dataplane_status"] == "unknown"
    assert record["status"] == "up"  # incomplete dig does not demote SystemUp
    assert case.issues[0]["id"] == iid
    assert case.issues[0]["status"] == "open"
    assert "svc1" not in dataplane_diagnosed_names(case)
    assert case.diagnoses[0]["complete"] is False


def test_incomplete_dataplane_issue_still_selectable_for_drill():
    from diagnostic_mas.case import open_issue
    from diagnostic_mas.dataplane_verify import (
        _incomplete_verify_finding,
        _record_incomplete_dataplane_verify,
    )
    from diagnostic_mas.drill import select_drill_issues

    case = CaseFile(
        budget=Budget(
            max_deep_checks=0,
            max_handoffs=0,
            max_drill_issues=2,
            max_tools_per_drill=12,
        )
    )
    open_issue(
        case,
        code="service_down",
        message="svc1",
        evidence_ids=[],
        layer="services",
        edge_id="svc1",
        dataplane_status="not_checked",
    )
    _record_incomplete_dataplane_verify(
        case,
        {
            "name": "svc1",
            "service_type": "l2sts",
            "system_status": "up",
            "dataplane_status": "not_checked",
        },
        _incomplete_verify_finding({}),
    )
    assert case.issues[0]["status"] == "open"
    picked = select_drill_issues(case, limit=2)
    assert [i.get("edge_id") for i in picked] == ["svc1"]


def test_open_issues_after_dataplane_are_explained():
    from diagnostic_mas.dataplane_verify import _open_issues_for_updated_services

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_evidence(
        case,
        {
            "kind": "dataplane_finding",
            "payload": {
                "name": "svc1",
                "dataplane_status": "down",
                "observed": "x",
                "cause": "y",
            },
        },
    )
    _open_issues_for_updated_services(
        case,
        {
            "l2sts/svc1": {
                "name": "svc1",
                "service_type": "l2sts",
                "status": "down",
                "system_status": "up",
                "dataplane_status": "down",
                "devices": ["renc-data-sw"],
            }
        },
    )
    assert len(case.issues) == 1
    assert case.issues[0]["status"] == "explained"


@pytest.mark.asyncio
async def test_llm_dataplane_verify_applies_status():
    from diagnostic_mas.dataplane_verify import llm_dataplane_verify_one

    class _Fn:
        def __init__(self, name, arguments, id_="1"):
            self.name = name
            self.arguments = arguments
            self.id = id_

    class _Call:
        def __init__(self, fn):
            self.function = fn
            self.id = fn.id

    class _Msg:
        def __init__(self, tool_calls=None, content=None):
            self.tool_calls = tool_calls
            self.content = content

    class _Choice:
        def __init__(self, message):
            self.message = message

    class _Resp:
        def __init__(self, message):
            self.choices = [_Choice(message)]

    class FakeOAI:
        def __init__(self):
            self.chat = self
            self.completions = self
            self.n = 0

        def create(self, **kwargs):
            self.n += 1
            if self.n == 1:
                return _Resp(
                    _Msg(
                        tool_calls=[
                            _Call(
                                _Fn(
                                    "conclude_dataplane",
                                    '{"dataplane_status":"down","observed":"xc missing","cause":"ac not in xconnect"}',
                                    id_="c1",
                                )
                            )
                        ]
                    )
                )
            return _Resp(_Msg(content="done"))

    case = CaseFile(
        budget=Budget(
            max_deep_checks=0,
            max_handoffs=0,
            max_drill_issues=2,
            max_tools_per_drill=5,
        )
    )
    rec = {
        "name": "svc1",
        "service_type": "l2sts",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "status": "up",
        "devices": ["renc-data-sw"],
    }
    session = DrillSession(max_tools=5, issue_edge_id="svc1")

    class S:
        fabric_model = "m"
        fabric_api_key = "k"
        fabric_api_url = "http://x"

    ok = await llm_dataplane_verify_one(
        object(),
        S(),
        case,
        record=rec,
        device_names={"renc-data-sw"},
        session=session,
        openai_client=FakeOAI(),
    )
    assert ok is True
    assert rec["dataplane_status"] == "down"
    assert rec["status"] == "down"
    findings = [e for e in case.evidence if e.get("kind") == "dataplane_finding"]
    assert len(findings) == 1


@pytest.mark.asyncio
async def test_llm_up_conclusion_gated_on_soft_live_l2():
    """l2ptp soft-error live_l2 rejects LLM up; l2sts uses both-PE config gates."""
    from diagnostic_mas.dataplane_verify import llm_dataplane_verify_one

    class _Fn:
        def __init__(self, name, arguments, id_="1"):
            self.name = name
            self.arguments = arguments
            self.id = id_

    class _Call:
        def __init__(self, fn):
            self.function = fn
            self.id = fn.id

    class _Msg:
        def __init__(self, tool_calls=None, content=None):
            self.tool_calls = tool_calls
            self.content = content

    class _Choice:
        def __init__(self, message):
            self.message = message

    class _Resp:
        def __init__(self, message):
            self.choices = [_Choice(message)]

    class FakeOAI:
        def __init__(self):
            self.chat = self
            self.completions = self

        def create(self, **kwargs):
            return _Resp(
                _Msg(
                    tool_calls=[
                        _Call(
                            _Fn(
                                "conclude_dataplane",
                                '{"dataplane_status":"up","observed":"xc up","cause":"path ok"}',
                                id_="c1",
                            )
                        )
                    ]
                )
            )

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    rec = {
        "name": "l2-ptp",
        "service_type": "l2ptp",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "status": "up",
        "live_l2": {
            "endpoints": [
                {"error": "ac_not_found", "ac": "Hu0/0/0/4.100"},
                {"error": "ac_not_found", "ac": "TF0/0/0/23/1.100"},
            ]
        },
    }
    session = DrillSession(max_tools=5)

    class S:
        fabric_model = "m"
        fabric_api_key = "k"
        fabric_api_url = "http://x"

    ok = await llm_dataplane_verify_one(
        object(),
        S(),
        case,
        record=rec,
        device_names=set(),
        session=session,
        openai_client=FakeOAI(),
    )
    assert ok is True
    assert rec["dataplane_status"] == "unknown"
    finding = next(
        e for e in case.evidence if e.get("kind") == "dataplane_incomplete"
    )
    assert finding["payload"]["source"] in {"llm", "gate"}
    assert "[gate]" in finding["payload"]["cause"]
    assert "ac_not_found" in finding["payload"]["cause"]


@pytest.mark.asyncio
async def test_llm_l2sts_conclusion_kept_even_if_xconnect_story():
    from diagnostic_mas.dataplane_verify import llm_dataplane_verify_one

    class _Fn:
        def __init__(self, name, arguments, id_="1"):
            self.name = name
            self.arguments = arguments
            self.id = id_

    class _Call:
        def __init__(self, fn):
            self.function = fn
            self.id = fn.id

    class _Msg:
        def __init__(self, tool_calls=None, content=None):
            self.tool_calls = tool_calls
            self.content = content

    class _Choice:
        def __init__(self, message):
            self.message = message

    class _Resp:
        def __init__(self, message):
            self.choices = [_Choice(message)]

    class FakeOAI:
        def __init__(self):
            self.chat = self
            self.completions = self

        def create(self, **kwargs):
            return _Resp(
                _Msg(
                    tool_calls=[
                        _Call(
                            _Fn(
                                "conclude_dataplane",
                                (
                                    '{"dataplane_status":"down",'
                                    '"observed":"no p2p xconnect for EVI 9001",'
                                    '"cause":"missing xconnect",'
                                    '"fix_suggestion":"configure p2p xconnect"}'
                                ),
                                id_="c1",
                            )
                        )
                    ]
                )
            )

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    rec = {
        "name": "l2-sts",
        "service_type": "l2sts",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "status": "up",
    }
    session = DrillSession(max_tools=5)

    class S:
        fabric_model = "m"
        fabric_api_key = "k"
        fabric_api_url = "http://x"

    ok = await llm_dataplane_verify_one(
        object(),
        S(),
        case,
        record=rec,
        device_names=set(),
        session=session,
        openai_client=FakeOAI(),
    )
    assert ok is True
    assert rec["dataplane_status"] == "down"
    finding = next(e for e in case.evidence if e.get("kind") == "dataplane_finding")
    assert finding["payload"]["cause"] == "missing xconnect"
    assert finding["payload"].get("fix_suggestion") == "configure p2p xconnect"


@pytest.mark.asyncio
async def test_llm_l2sts_up_demoted_without_session_evidence():
    from diagnostic_mas.dataplane_verify import llm_dataplane_verify_one

    class _Fn:
        def __init__(self, name, arguments, id_="1"):
            self.name = name
            self.arguments = arguments
            self.id = id_

    class _Call:
        def __init__(self, fn):
            self.function = fn
            self.id = fn.id

    class _Msg:
        def __init__(self, tool_calls=None, content=None):
            self.tool_calls = tool_calls
            self.content = content

    class _Choice:
        def __init__(self, message):
            self.message = message

    class _Resp:
        def __init__(self, message):
            self.choices = [_Choice(message)]

    class FakeOAI:
        def __init__(self):
            self.chat = self
            self.completions = self

        def create(self, **kwargs):
            return _Resp(
                _Msg(
                    tool_calls=[
                        _Call(
                            _Fn(
                                "conclude_dataplane",
                                (
                                    '{"dataplane_status":"up",'
                                    '"observed":"EVI 9001 RT import/export 398900:9001 both PEs",'
                                    '"cause":"route-targets match on both ends"}'
                                ),
                                id_="c1",
                            )
                        )
                    ]
                )
            )

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    rec = {
        "name": "l2-sts",
        "service_type": "l2sts",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "status": "up",
        "devices": ["lbnl-data-sw", "renc-data-sw"],
    }
    session = DrillSession(max_tools=5)

    class S:
        fabric_model = "m"
        fabric_api_key = "k"
        fabric_api_url = "http://x"

    ok = await llm_dataplane_verify_one(
        object(),
        S(),
        case,
        record=rec,
        device_names=set(),
        session=session,
        openai_client=FakeOAI(),
    )
    assert ok is True
    assert rec["dataplane_status"] == "unknown"
    assert "[gate]" in case.diagnoses[0]["cause"]
    assert case.diagnoses[0]["complete"] is False
    from diagnostic_mas.dataplane_verify import dataplane_diagnosed_names

    assert "l2-sts" not in dataplane_diagnosed_names(case)
    assert any(e.get("kind") == "dataplane_incomplete" for e in case.evidence)


@pytest.mark.asyncio
async def test_llm_l2ptp_up_rejected_when_ac_not_found():
    """Soft-error live_l2 must not accept LLM up."""
    from diagnostic_mas.dataplane_verify import llm_dataplane_verify_one

    class _Fn:
        def __init__(self, name, arguments, id_="1"):
            self.name = name
            self.arguments = arguments
            self.id = id_

    class _Call:
        def __init__(self, fn):
            self.function = fn
            self.id = fn.id

    class _Msg:
        def __init__(self, tool_calls=None, content=None):
            self.tool_calls = tool_calls
            self.content = content

    class _Choice:
        def __init__(self, message):
            self.message = message

    class _Resp:
        def __init__(self, message):
            self.choices = [_Choice(message)]

    class FakeOAI:
        def __init__(self):
            self.chat = self
            self.completions = self

        def create(self, **kwargs):
            return _Resp(
                _Msg(
                    tool_calls=[
                        _Call(
                            _Fn(
                                "conclude_dataplane",
                                '{"dataplane_status":"up","observed":"other xc UP","cause":"xc up"}',
                                id_="c1",
                            )
                        )
                    ]
                )
            )

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    rec = {
        "name": "l2-ptp",
        "service_type": "l2ptp",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "status": "up",
        "live_l2": {
            "endpoints": [
                {"error": "ac_not_found", "ac": "Hu0/0/0/4.100"},
                {"error": "ac_not_found", "ac": "TF0/0/0/23/1.100"},
            ]
        },
    }
    session = DrillSession(max_tools=5)

    class S:
        fabric_model = "m"
        fabric_api_key = "k"
        fabric_api_url = "http://x"

    ok = await llm_dataplane_verify_one(
        object(),
        S(),
        case,
        record=rec,
        device_names=set(),
        session=session,
        openai_client=FakeOAI(),
    )
    assert ok is True
    assert rec["dataplane_status"] == "unknown"
    assert "[gate]" in case.diagnoses[0]["cause"]


@pytest.mark.asyncio
async def test_llm_no_conclude_uses_fallback():
    from diagnostic_mas.dataplane_verify import llm_dataplane_verify_one

    class _Msg:
        def __init__(self, tool_calls=None, content=None):
            self.tool_calls = tool_calls
            self.content = content

    class _Choice:
        def __init__(self, message):
            self.message = message

    class _Resp:
        def __init__(self, message):
            self.choices = [_Choice(message)]

    class FakeOAI:
        def __init__(self):
            self.chat = self
            self.completions = self

        def create(self, **kwargs):
            return _Resp(_Msg(content="still looking around..."))

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    rec = {
        "name": "l2-sts",
        "service_type": "l2sts",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "status": "up",
        "live_l2": {
            "endpoints": [
                {"error": "ac_not_found"},
                {"error": "ac_not_found"},
            ]
        },
    }
    session = DrillSession(max_tools=3)

    class S:
        fabric_model = "m"
        fabric_api_key = "k"
        fabric_api_url = "http://x"

    ok = await llm_dataplane_verify_one(
        object(),
        S(),
        case,
        record=rec,
        device_names=set(),
        session=session,
        openai_client=FakeOAI(),
    )
    assert ok is True
    assert rec["dataplane_status"] == "unknown"
    incomplete = next(
        e for e in case.evidence if e.get("kind") == "dataplane_incomplete"
    )
    assert incomplete["payload"]["source"] == "fallback"
    assert incomplete["payload"]["dataplane_status"] == "unknown"
    assert incomplete["payload"]["complete"] is False
    assert "incomplete" in incomplete["payload"]["cause"].lower()
    assert "LLM did not conclude" in incomplete["payload"]["observed"]
    assert case.diagnoses[0]["status"] == "unknown"
    assert case.diagnoses[0]["complete"] is False
    from diagnostic_mas.dataplane_verify import dataplane_diagnosed_names

    assert "l2-sts" not in dataplane_diagnosed_names(case)


_LBNL_L2VPN = """
evpn
 evi 9001
  bgp
   route-target import 398900:9001
   route-target export 398900:9001
 !
!
l2vpn
 bridge group bg-l2-STS-25b0e3a0-364b-4b09-ab
  bridge-domain bd-l2-STS-25b0e3a0-364b-4b0
   interface TwentyFiveGigE0/0/0/23/1.100
   evi 9001
 !
!
"""


def test_clip_dataplane_tool_content_keeps_rts():
    from diagnostic_mas.dataplane_verify import clip_dataplane_tool_content

    noise = "x" * 50_000
    blob = noise + _LBNL_L2VPN + noise
    clipped = clip_dataplane_tool_content(
        "get_device_config",
        blob,
        service_name="l2-STS-25b0e3a0-364b-4b09-ab78-e7cc83febed1",
        limit=4000,
    )
    assert len(clipped) < len(blob)
    assert "398900:9001" in clipped
    assert "evi 9001" in clipped.lower() or "evi 9001" in clipped


def test_clip_dataplane_unwraps_mcp_envelope_and_preserves_newlines():
    from diagnostic_mas.dataplane_verify import clip_dataplane_tool_content

    envelope = {
        "status": "success",
        "data": {
            "result": (
                "Mon Sep 16\n"
                "Bridge Group: bg-demo\n"
                "  Bridge-domain: bd-demo\n"
                "    EVPN, State: up\n"
                "    AC: HundredGigE0/0/0/4.100, State: up\n"
            )
        },
    }
    out = clip_dataplane_tool_content(
        "exec_show",
        json.dumps(envelope),
        service_name="demo",
        limit=2000,
    )
    assert '"status"' not in out
    assert "Bridge Group: bg-demo" in out
    assert "\n" in out
    assert out.count("\n") >= 3


def test_clip_dataplane_marks_truncation_clearly():
    from diagnostic_mas.dataplane_verify import clip_dataplane_tool_content

    body = "line\n" * 5000
    out = clip_dataplane_tool_content("exec_show", body, limit=200)
    assert "[truncated" in out
    assert "of " in out


def test_deep_checks_truncate_does_not_str_dict_envelope():
    from multi_agent.deep_checks import _truncate

    cli = "A\n" * 3000
    result = {"status": "success", "data": {"result": cli}}
    out = _truncate(result, limit=500)
    assert isinstance(out, str)
    assert '"truncated"' not in out
    assert "preview" not in out or "[truncated" in out
    assert "\n" in out
    assert "[truncated:" in out


def test_deep_checks_evidence_stores_full_body():
    """Stored dig evidence keeps full MCP body; clip helpers are separate."""
    from multi_agent.deep_checks import _evidence_result, _truncate

    cli = "route-target import 398900:9001\n" + ("line\n" * 5000)
    result = {"status": "success", "data": {"result": cli}}
    stored = _evidence_result(result)
    assert isinstance(stored, str)
    assert "route-target import 398900:9001" in stored
    assert "[truncated" not in stored
    assert len(stored) >= len(cli)

    clipped = _truncate(result, limit=400)
    assert "[truncated:" in clipped
    assert len(clipped) < len(stored)


@pytest.mark.asyncio
async def test_timeout_uses_generic_fallback(monkeypatch):
    """LLM timeout after tools → fallback source, not rt_extract."""
    from diagnostic_mas import dataplane_verify as dv

    class _Fn:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class _Call:
        def __init__(self, id_, name, arguments):
            self.id = id_
            self.function = _Fn(name, arguments)

    class _Msg:
        def __init__(self, tool_calls=None, content=None):
            self.tool_calls = tool_calls
            self.content = content

    class _Choice:
        def __init__(self, message):
            self.message = message

    class _Resp:
        def __init__(self, message):
            self.choices = [_Choice(message)]

    class FakeOAI:
        def __init__(self):
            self.chat = self
            self.completions = self
            self.n = 0

        def create(self, **kwargs):
            self.n += 1
            if self.n == 1:
                return _Resp(
                    _Msg(
                        tool_calls=[
                            _Call(
                                "1",
                                "mcp_call",
                                json.dumps(
                                    {
                                        "tool_name": "get_device_config",
                                        "params": {"device": "lbnl-data-sw"},
                                    }
                                ),
                            ),
                        ]
                    )
                )
            raise TimeoutError("Request timed out.")

    async def fake_exec(client, case, **kwargs):
        return _LBNL_L2VPN

    monkeypatch.setattr(dv, "execute_one_drill_call", fake_exec)

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    rec = {
        "name": "l2-STS-25b0e3a0-364b-4b09-ab78-e7cc83febed1",
        "service_type": "l2sts",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "devices": ["lbnl-data-sw", "renc-data-sw"],
    }
    session = DrillSession(max_tools=20)

    class S:
        fabric_model = "m"
        fabric_api_key = "k"
        fabric_api_url = "http://x"

    ok = await dv.llm_dataplane_verify_one(
        object(),
        S(),
        case,
        record=rec,
        device_names={"lbnl-data-sw", "renc-data-sw"},
        session=session,
        openai_client=FakeOAI(),
    )
    assert ok is True
    finding = next(e for e in case.evidence if e.get("kind") == "dataplane_incomplete")
    assert finding["payload"]["source"] == "fallback"
    assert finding["payload"]["dataplane_status"] == "unknown"
    assert finding["payload"]["complete"] is False
    assert case.diagnoses[0]["complete"] is False
    assert rec["dataplane_status"] == "unknown"


def test_empty_tool_turn_log_line_snips_content():
    from diagnostic_mas.dataplane_verify import (
        _empty_tool_turn_log_line,
        _snip_llm_content_for_log,
    )

    class Msg:
        content = "PE looks fine\nno fault"

    line = _empty_tool_turn_log_line(Msg(), finish_reason="stop")
    assert "finish_reason=stop" in line
    assert "PE looks fine" in line
    assert "\\n" in line
    empty = _empty_tool_turn_log_line(type("M", (), {"content": None})())
    assert "content=<empty>" in empty
    assert len(_snip_llm_content_for_log("a" * 500, limit=80)) <= 80


@pytest.mark.asyncio
async def test_empty_tool_turn_nudge_then_conclude(monkeypatch, capsys):
    """Prose-only turn → one conclude nudge → conclude_dataplane succeeds."""
    from diagnostic_mas import dataplane_verify as dv

    class _Fn:
        def __init__(self, name, arguments, id_="1"):
            self.name = name
            self.arguments = arguments
            self.id = id_

    class _Call:
        def __init__(self, fn):
            self.function = fn
            self.id = fn.id

    class _Msg:
        def __init__(self, tool_calls=None, content=None):
            self.tool_calls = tool_calls
            self.content = content

    class _Choice:
        def __init__(self, message, finish_reason="stop"):
            self.message = message
            self.finish_reason = finish_reason

    class _Resp:
        def __init__(self, message, finish_reason="stop"):
            self.choices = [_Choice(message, finish_reason=finish_reason)]

    class FakeOAI:
        def __init__(self):
            self.chat = self
            self.completions = self
            self.n = 0
            self.tool_choices = []

        def create(self, **kwargs):
            self.n += 1
            self.tool_choices.append(kwargs.get("tool_choice"))
            if self.n == 1:
                return _Resp(
                    _Msg(
                        tool_calls=[
                            _Call(
                                _Fn(
                                    "mcp_call",
                                    json.dumps(
                                        {
                                            "tool_name": "exec_show",
                                            "params": {
                                                "device_name": "amst-data-sw",
                                                "input_command": "interfaces Hu0/0/0/5.2036",
                                            },
                                        }
                                    ),
                                    id_="t1",
                                )
                            )
                        ]
                    )
                )
            if self.n == 2:
                return _Resp(
                    _Msg(
                        tool_calls=None,
                        content="Looks up; no more tools needed.",
                    ),
                    finish_reason="stop",
                )
            return _Resp(
                _Msg(
                    tool_calls=[
                        _Call(
                            _Fn(
                                "conclude_dataplane",
                                (
                                    '{"dataplane_status":"up",'
                                    '"observed":"AC up on amst-data-sw",'
                                    '"cause":"PE-side ready; traffic delivery unverified"}'
                                ),
                                id_="c1",
                            )
                        )
                    ]
                )
            )

    async def fake_exec(client, case, **kwargs):
        return "HundredGigE0/0/0/5.2036 is up, line protocol is up"

    monkeypatch.setattr(dv, "execute_one_drill_call", fake_exec)

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    rec = {
        "name": "svc-nudge",
        "service_type": "l3rt",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "devices": ["amst-data-sw"],
    }
    session = DrillSession(max_tools=20)

    class S:
        fabric_model = "m"
        fabric_api_key = "k"
        fabric_api_url = "http://x"

    oai = FakeOAI()
    ok = await dv.llm_dataplane_verify_one(
        object(),
        S(),
        case,
        record=rec,
        device_names={"amst-data-sw"},
        session=session,
        openai_client=oai,
    )
    assert ok is True
    assert oai.n == 3
    assert oai.tool_choices[0] == "auto"
    assert oai.tool_choices[1] == "auto"
    assert isinstance(oai.tool_choices[2], dict)
    assert oai.tool_choices[2]["function"]["name"] == "conclude_dataplane"
    assert rec["dataplane_status"] == "up"
    err = capsys.readouterr().err
    assert "finish_reason=stop" in err
    assert "Looks up" in err
    assert "nudging once: force conclude_dataplane" in err
    assert "stop (after nudge)" not in err


@pytest.mark.asyncio
async def test_empty_tool_turn_nudge_then_still_empty(monkeypatch, capsys):
    """Two prose-only turns → incomplete no_conclusion after nudge."""
    from diagnostic_mas import dataplane_verify as dv

    class _Msg:
        def __init__(self, content=None):
            self.tool_calls = None
            self.content = content

    class _Choice:
        def __init__(self, message, finish_reason="stop"):
            self.message = message
            self.finish_reason = finish_reason

    class _Resp:
        def __init__(self, message):
            self.choices = [_Choice(message)]

    class FakeOAI:
        def __init__(self):
            self.chat = self
            self.completions = self
            self.n = 0

        def create(self, **kwargs):
            self.n += 1
            return _Resp(_Msg(content=f"prose round {self.n}"))

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    rec = {
        "name": "svc-empty",
        "service_type": "l3rt",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "devices": ["amst-data-sw"],
    }
    session = DrillSession(max_tools=20)

    class S:
        fabric_model = "m"
        fabric_api_key = "k"
        fabric_api_url = "http://x"

    ok = await dv.llm_dataplane_verify_one(
        object(),
        S(),
        case,
        record=rec,
        device_names={"amst-data-sw"},
        session=session,
        openai_client=FakeOAI(),
    )
    assert ok is True
    assert rec["dataplane_status"] == "unknown"
    dx = case.diagnoses[0]
    assert dx["complete"] is False
    assert "neither tools nor conclude_dataplane" in dx["observed"]
    err = capsys.readouterr().err
    assert "nudging once: force conclude_dataplane" in err
    assert "stop (after nudge)" in err
    assert "prose round" in err
