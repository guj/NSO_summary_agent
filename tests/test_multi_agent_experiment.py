"""Unit tests for multi-agent experiment helpers."""

from multi_agent.base import (
    AgentResult,
    dedupe_issues,
    format_domain_section,
    gate_plan,
    parse_plan_json,
)
from multi_agent.merge import build_merged_report, merge_results


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


def test_parse_plan_json_from_fenced_noise():
    tasks = parse_plan_json(
        '[{"check":"exec_show","args":{"device":"a","command":"bgp summary"}}]'
    )
    assert len(tasks) == 1
    assert tasks[0]["check"] == "exec_show"


def test_format_and_merge_reports_issues():
    isis = AgentResult(
        name="isis",
        layer="underlay",
        operational_summary={
            "total": 2,
            "up": 1,
            "down": 0,
            "unidirectional": 1,
            "unknown": 0,
        },
        issues=[
            {
                "severity": "high",
                "layer": "underlay",
                "code": "unidirectional_adjacency",
                "edge_id": "isis:a:Gi0:b:Gi0",
                "message": "a sees b; b does not",
            }
        ],
    )
    bgp = AgentResult(
        name="bgp",
        layer="routing",
        operational_summary={
            "total": 1,
            "up": 0,
            "down": 0,
            "degraded": 1,
            "unknown": 0,
        },
        issues=[
            {
                "severity": "medium",
                "layer": "routing",
                "code": "missing_reverse_session",
                "edge_id": None,
                "message": "no reverse",
            }
        ],
    )
    merged = merge_results([isis, bgp])
    assert merged["issues_total"] == 2
    report = build_merged_report([isis, bgp])
    assert "unidirectional_adjacency" in report
    assert "missing_reverse_session" in report
    assert "IS-IS connectivity" in report
    assert "BGP peering" in report
    section = format_domain_section(isis)
    assert "unidirectional=1" in section


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


def test_topology_slice_and_device_report():
    from multi_agent.topology_slice import (
        devices_with_topology_issues,
        slice_topology_for_device,
    )
    from multi_agent.device_agent import format_device_section

    isis = AgentResult(
        name="isis",
        layer="underlay",
        static_edges=[
            {
                "id": "isis:a:Gi0:b:Gi0",
                "local": {"device": "a", "interface": "Gi0"},
                "remote": {"device": "b", "interface": "Gi0"},
            }
        ],
        operational_edges=[
            {"id": "isis:a:Gi0:b:Gi0", "state": {"status": "unidirectional"}}
        ],
        issues=[
            {
                "code": "unidirectional_adjacency",
                "edge_id": "isis:a:Gi0:b:Gi0",
                "message": "a↔b unidirectional",
                "severity": "high",
            }
        ],
    )
    bgp = AgentResult(name="bgp", layer="routing")
    seed = slice_topology_for_device("a", isis, bgp)
    assert len(seed["isis"]["operational_edges"]) == 1
    assert seed["isis"]["issues"]
    assert devices_with_topology_issues(isis, bgp, ["a", "b", "c"]) == ["a", "b"]

    device = AgentResult(
        name="device:a",
        layer="device",
        seed=seed,
        hardware={"outcome": "ok"},
        issues=seed["isis"]["issues"],
    )
    report = build_merged_report([isis, bgp, device])
    assert "## Devices" in report
    assert "### a" in report or "device:a" in format_device_section(device)
    assert "Overall Status" in report


def test_executive_header_at_top():
    from multi_agent.executive import format_executive_header

    # One BGP edge touching a+b, one ISIS edge touching a+b → fleet totals 2/2
    # (per-device sum, same as production Fleet Summary).
    bgp_static = {
        "id": "bgp:1.1.1.1:2.2.2.2:a:b",
        "local": {"device": "a", "address": "1.1.1.1"},
        "remote": {"device": "b", "address": "2.2.2.2"},
    }
    bgp_op = {
        "id": "bgp:1.1.1.1:2.2.2.2:a:b",
        "state": {"status": "up"},
        "local": {"device": "a", "address": "1.1.1.1"},
        "remote": {"device": "b", "address": "2.2.2.2"},
    }
    isis_static = {
        "id": "isis:a:Gi0:b:Gi0",
        "local": {"device": "a", "interface": "Gi0"},
        "remote": {"device": "b", "interface": "Gi0"},
    }
    isis_op = {
        "id": "isis:a:Gi0:b:Gi0",
        "state": {"status": "up"},
        "local": {"device": "a", "interface": "Gi0"},
        "remote": {"device": "b", "interface": "Gi0"},
    }
    isis = AgentResult(
        name="isis",
        layer="underlay",
        static_edges=[isis_static],
        operational_edges=[isis_op],
        operational_summary={
            "total": 1,
            "up": 1,
            "down": 0,
            "unidirectional": 0,
            "unknown": 0,
        },
        issues=[],
    )
    bgp = AgentResult(
        name="bgp",
        layer="routing",
        static_edges=[bgp_static],
        operational_edges=[bgp_op],
        operational_summary={
            "total": 1,
            "up": 1,
            "down": 0,
            "degraded": 0,
            "unknown": 0,
        },
        issues=[],
    )
    header = format_executive_header(
        [isis, bgp],
        run_id="20260729T175000Z",
        summary_text="Fleet needs review.",
        fabric_api_url="https://ai.fabric-testbed.net",
        fabric_model="gpt-oss-20b",
        mcp_server_cmd="cisco-nso-mcp-server",
        llm_skipped=True,
    )
    assert header.startswith("====")
    assert "Overall Status" in header
    assert "LLM: skipped" in header
    assert "MCP: cisco-nso-mcp-server" in header
    assert "Fleet Summary" in header
    assert "Action Items" in header
    assert "Operational Assessment" in header
    assert "BGP Peers: 2/2 Established" in header
    assert "IS-IS Adjacencies: 2/2 Up" in header
    report = build_merged_report([isis, bgp], run_id="20260729T175000Z")
    assert report.index("Overall Status") < report.index("IS-IS connectivity")


def test_fleet_pack_executive_has_service_and_device_health():
    from multi_agent.executive import format_executive_header
    from multi_agent.fleet_spine import (
        assemble_topology,
        merge_device_feedback,
    )

    isis = AgentResult(
        name="isis",
        layer="underlay",
        static_edges=[
            {
                "id": "isis:a:Gi0:b:Gi0",
                "local": {"device": "a", "interface": "Gi0"},
                "remote": {"device": "b", "interface": "Gi0"},
            }
        ],
        operational_edges=[
            {
                "id": "isis:a:Gi0:b:Gi0",
                "state": {"status": "up"},
                "local": {"device": "a", "interface": "Gi0"},
                "remote": {"device": "b", "interface": "Gi0"},
            }
        ],
        operational_summary={
            "total": 1,
            "up": 1,
            "down": 0,
            "unidirectional": 0,
            "unknown": 0,
        },
    )
    bgp = AgentResult(name="bgp", layer="routing")
    phys = [
        {
            "id": "phys:a:Gi0:b:Gi0",
            "local": {"device": "a", "interface": "Gi0"},
            "remote": {"device": "b", "interface": "Gi0"},
        }
    ]
    topo = assemble_topology(
        device_names=["a", "b"],
        physical_edges=phys,
        physical_op_edges=[
            {
                "id": "phys:a:Gi0:b:Gi0",
                "local": {"device": "a", "interface": "Gi0", "oper_status": "up"},
                "remote": {"device": "b", "interface": "Gi0", "oper_status": "up"},
                "state": {"status": "up"},
            }
        ],
        isis=isis,
        bgp=bgp,
    )
    fleet = {
        "counts": {"l2ptp": {"total": 1, "up": 1, "down": 0, "degraded": 0, "unknown": 0}},
        "fleet_sync": {
            "status": "success",
            "data": {
                "devices": [
                    {"name": "a", "sync_state": "in-sync"},
                    {"name": "b", "sync_state": "in-sync"},
                ]
            },
        },
        "system_health": {
            "a": {"cpu": {"one_min": 10, "five_min": 10, "fifteen_min": 10}},
            "b": {"cpu": {"one_min": 5, "five_min": 5, "fifteen_min": 5}},
        },
        "hardware_health": {"a": {"outcome": "ok"}, "b": {"outcome": "ok"}},
        "topology": topo,
        "delta": {
            "first_run": False,
            "counts": {},
            "new_failures": [],
            "recoveries": [],
            "status_changes": [],
            "removed": [],
        },
    }
    header = format_executive_header(
        [isis, bgp], run_id="20260729T181900Z", fleet_pack=fleet
    )
    assert "Service Summary" in header
    assert "l2ptp" in header
    assert "Device Health" in header
    assert "Changes Since Last Report" in header
    assert "In Sync:" in header
    assert "CPU Alerts:" in header
    assert "Devices requiring review:" in header
    assert "not collected in this multi-agent run" not in header

    report = build_merged_report([isis, bgp], run_id="20260729T181900Z", fleet_pack=fleet)
    assert "Service Summary" in report
    assert report.index("Service Summary") < report.index("Detailed Analysis")

    merged = merge_device_feedback(
        {
            "hardware_health": {"a": {"outcome": "stale"}},
            "system_health": {"a": {"cpu": {"one_min": 1}}},
        },
        [
            AgentResult(
                name="device:a",
                layer="device",
                seed={"device": "a", "system_health": {"cpu": {"one_min": 99}}},
                hardware={"outcome": "fresh"},
            )
        ],
    )
    assert merged["hardware_health"]["a"]["outcome"] == "fresh"
    assert merged["system_health"]["a"]["cpu"]["one_min"] == 99


def test_metrics_snapshot_from_run_uses_fleet_or_topology():
    from agent.metrics import build_phase1_metrics
    from multi_agent.orchestrator import metrics_snapshot_from_run

    isis = AgentResult(
        name="isis",
        layer="underlay",
        static_edges=[
            {
                "id": "isis:a:Gi0:b:Gi0",
                "local": {"device": "a", "interface": "Gi0"},
                "remote": {"device": "b", "interface": "Gi0"},
            }
        ],
        operational_edges=[
            {
                "id": "isis:a:Gi0:b:Gi0",
                "state": {"status": "up"},
                "local": {"device": "a", "interface": "Gi0"},
                "remote": {"device": "b", "interface": "Gi0"},
            }
        ],
        operational_summary={"total": 1, "up": 1, "down": 0},
    )
    bgp = AgentResult(name="bgp", layer="routing")
    snap = metrics_snapshot_from_run(
        fleet_pack=None,
        device_names=["a", "b"],
        physical_edges=[],
        isis=isis,
        bgp=bgp,
    )
    assert snap["topology"] is not None
    lines = build_phase1_metrics(snap, success=True, duration_seconds=1.5)
    text = "\n".join(lines)
    assert "nso_isis_adjacencies_up" in text
    assert "nso_summary_run_success" in text

    with_fleet = metrics_snapshot_from_run(
        fleet_pack={
            "topology": snap["topology"],
            "fleet_sync": {"status": "success"},
            "counts": {"l2ptp": {"up": 1, "down": 0, "degraded": 0, "unknown": 0}},
        },
        device_names=["a", "b"],
        physical_edges=[],
        isis=isis,
        bgp=bgp,
    )
    assert with_fleet["counts"]["l2ptp"]["up"] == 1
