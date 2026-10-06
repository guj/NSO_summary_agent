"""Unit tests for multi-agent experiment helpers."""

from multi_agent.base import (
    AgentResult,
    dedupe_issues,
    format_domain_section,
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


def test_no_issues_message():
    empty = AgentResult(
        name="isis",
        layer="underlay",
        operational_summary={"total": 0, "up": 0, "down": 0, "unidirectional": 0},
    )
    assert "No bidirectional issues detected" in format_domain_section(empty)


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


def test_isis_evidence_summary_skips_timestamps():
    from multi_agent.base import format_evidence_preview

    show = (
        "Tue Aug 11 21:49:40.200 UTC\n\n"
        "IS-IS fabric-net neighbors:\n"
        "System Id      Interface        SNPA           State Holdtime Type IETF-NSF\n"
        "star-data-sw   Hu0/0/0/23.856   *PtoP*         Up    28       L2   Capable\n"
        "lbnl-data-sw   Hu0/0/0/23.851   *PtoP*         Up    28       L2   Capable\n"
    )
    text = "\n".join(
        format_evidence_preview(
            [
                {
                    "check": "exec_show",
                    "args": {
                        "device_name": "uky-data-sw",
                        "input_command": "isis neighbors",
                    },
                    "result": {
                        "device": "uky-data-sw",
                        "command": "show isis neighbors",
                        "result": show,
                    },
                }
            ]
        )
    )
    assert "Tue@" not in text
    assert "star-data-sw@Hu0/0/0/23.856=Up" in text
    assert "lbnl-data-sw@Hu0/0/0/23.851=Up" in text


def test_evidence_preview_in_report():
    from multi_agent.base import format_evidence_preview

    lines = format_evidence_preview(
        [
            {
                "check": "exec_show",
                "args": {
                    "device_name": "lbnl-data-sw",
                    "input_command": "bgp summary",
                },
                "reason": "verify BGP neighbor mapping",
                "result": {
                    "device": "lbnl-data-sw",
                    "command": "show bgp summary",
                    "result": (
                        "BGP router identifier 10.129.0.1, local AS number 398900\n"
                        "Neighbor        Spk    AS MsgRcvd MsgSent   TblVer  InQ OutQ  Up/Down  St/PfxRcd\n"
                        "10.128.0.1        0 398900   17231   17225  5752936    0    0     1w4d          0\n"
                        "10.148.0.1        0 398900       0   45374        0    0    0 00:00:00 Idle\n"
                    ),
                },
            },
            {
                "check": "verify_bgp_peer_reachability",
                "args": {"device_name": "lbnl-data-sw"},
                "error": "Missing required parameter: device_name",
            },
        ]
    )
    text = "\n".join(lines)
    assert "Deep-check evidence (2): ok=1 err=1" in text
    assert "ok  lbnl-data-sw  exec_show 'bgp summary'" in text
    assert "peers:" in text
    assert "10.148.0.1=Idle" in text
    assert "10.128.0.1=Est/0" in text
    assert "err  lbnl-data-sw  verify_bgp_peer_reachability" in text
    assert "Missing required parameter" in text
    assert "result: {" not in text
