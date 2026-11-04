"""Unit tests for multi-agent experiment helpers."""

from multi_agent.base import (
    dedupe_issues,
    gate_plan,
    parse_plan_json,
)


def test_dedupe_issues_keeps_first_unique():
    issues = [
        {
            "severity": "medium",
            "layer": "routing",
            "code": "unknown_neighbor_address",
            "edge_id": None,
            "message": "lbnl-data-sw 10.148.0.1: could not map neighbor address to NSO device",
        },
        {
            "severity": "medium",
            "layer": "routing",
            "code": "unknown_neighbor_address",
            "edge_id": None,
            "message": "lbnl-data-sw 10.148.0.1: could not map neighbor address to NSO device",
        },
        {
            "severity": "medium",
            "layer": "routing",
            "code": "unknown_neighbor_address",
            "edge_id": None,
            "message": "uky-data-sw 10.133.0.1: could not map neighbor address to NSO device",
        },
    ]
    out = dedupe_issues(issues)
    assert len(out) == 2
    assert out[0]["message"].startswith("lbnl-data-sw")
    assert out[1]["message"].startswith("uky-data-sw")


def test_gate_plan_rejects_unknown_tool_and_device():
    tasks = [
        {"check": "rm_rf", "args": {"device": "a"}},
        {"check": "exec_show", "args": {"device": "ghost", "command": "isis neighbors"}},
        {
            "check": "exec_show",
            "args": {"device": "lbnl-data-sw", "command": "isis neighbors"},
            "reason": "ok",
        },
    ]
    out = gate_plan(
        tasks,
        allowlist=frozenset({"exec_show"}),
        device_names={"lbnl-data-sw", "renc-data-sw"},
    )
    assert len(out) == 1
    assert out[0]["args"]["device"] == "lbnl-data-sw"


def test_gate_plan_rejects_ping_via_exec_show_and_bad_service_sync():
    from multi_agent.base import task_rejection_reason

    allow = frozenset({"exec_show", "check_service_sync"})
    devices = {"atla-data-sw", "star-data-sw"}
    assert "ping" in (
        task_rejection_reason(
            "exec_show",
            {"device_name": "atla-data-sw", "input_command": "ping 10.1.1.1"},
            allowlist=allow,
            device_names=devices,
        )
        or ""
    ).lower()
    assert "service_type" in (
        task_rejection_reason(
            "check_service_sync",
            {"device_name": "star-data-sw"},
            allowlist=allow,
            device_names=devices,
        )
        or ""
    ).lower()
    out = gate_plan(
        [
            {
                "check": "exec_show",
                "args": {
                    "device_name": "atla-data-sw",
                    "input_command": "ping 10.1.1.1",
                },
            },
            {
                "check": "check_service_sync",
                "args": {"device_name": "star-data-sw"},
            },
            {
                "check": "check_service_sync",
                "args": {"service_type": "l2ptp", "service_name": "svc1"},
            },
            {
                "check": "exec_show",
                "args": {
                    "device_name": "atla-data-sw",
                    "input_command": "bgp summary",
                },
            },
        ],
        allowlist=allow,
        device_names=devices,
    )
    assert len(out) == 2
    assert {t["check"] for t in out} == {"check_service_sync", "exec_show"}


def test_gate_plan_rejects_quarantined_device(monkeypatch):
    monkeypatch.setattr(
        "nso_facts.mcp_client.is_device_quarantined",
        lambda d: d == "star-data-sw",
    )
    out = gate_plan(
        [
            {
                "check": "exec_show",
                "args": {
                    "device_name": "star-data-sw",
                    "input_command": "bgp summary",
                },
            },
            {
                "check": "exec_show",
                "args": {
                    "device_name": "atla-data-sw",
                    "input_command": "bgp summary",
                },
            },
        ],
        allowlist=frozenset({"exec_show"}),
        device_names={"star-data-sw", "atla-data-sw"},
    )
    assert len(out) == 1
    assert out[0]["args"]["device_name"] == "atla-data-sw"


def test_parse_plan_json_from_fenced_noise():
    tasks = parse_plan_json(
        '[{"check":"exec_show","args":{"device":"a","command":"bgp summary"}}]'
    )
    assert len(tasks) == 1
    assert tasks[0]["check"] == "exec_show"


def test_gate_plan_force_device_rewrites():
    tasks = [
        {
            "check": "exec_show",
            "args": {"device": "evil", "command": "environment"},
        },
        {"check": "get_hardware_health", "args": {}},
    ]
    out = gate_plan(
        tasks,
        allowlist=frozenset({"exec_show", "get_hardware_health"}),
        device_names={"lbnl-data-sw"},
        force_device="lbnl-data-sw",
    )
    assert len(out) == 2
    assert out[0]["args"]["device"] == "lbnl-data-sw"
    assert out[1]["args"]["device_name"] == "lbnl-data-sw"
