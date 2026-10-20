from diagnostic_mas.case import Budget, CaseFile, add_evidence
from diagnostic_mas.device_health import (
    format_device_health_section,
    topology_from_case,
)
from diagnostic_mas.report import render_report


def _case_with_spines() -> CaseFile:
    case = CaseFile(
        budget=Budget(max_deep_checks=0, max_handoffs=0),
        device_names=["renc-data-sw", "lbnl-data-sw"],
    )
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "physical",
            "layer": "physical",
            "payload": {
                "static_edges": [
                    {
                        "id": "e1",
                        "local": {
                            "device": "renc-data-sw",
                            "interface": "Hu0/0/0/0",
                        },
                        "remote": {
                            "device": "lbnl-data-sw",
                            "interface": "Hu0/0/0/1",
                        },
                        "state": {"status": "up"},
                    }
                ],
                "operational_edges": [],
            },
        },
    )
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "isis",
            "layer": "underlay",
            "payload": {
                "static_edges": [],
                "operational_edges": [
                    {
                        "id": "u1",
                        "local": {"device": "renc-data-sw", "interface": "Hu0/0/0/0"},
                        "remote": {"device": "lbnl-data-sw", "interface": "Hu0/0/0/1"},
                        "state": {
                            "local": "up",
                            "remote": "up",
                            "status": "up",
                        },
                    }
                ],
                "operational_summary": {"up": 1, "down": 0},
            },
        },
    )
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "bgp",
            "layer": "routing",
            "payload": {
                "static_edges": [],
                "operational_edges": [
                    {
                        "id": "r1",
                        "local": {"device": "renc-data-sw", "address": "10.0.0.1"},
                        "remote": {"device": "lbnl-data-sw", "address": "10.0.0.2"},
                        "state": {
                            "local": "established",
                            "remote": "established",
                            "status": "up",
                        },
                    }
                ],
                "operational_summary": {"up": 1, "down": 0},
            },
        },
    )
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "operational_summary": {},
                "extra": {
                    "fleet_sync": {
                        "status": "success",
                        "data": {
                            "devices": [
                                {"device": "renc-data-sw", "result": "in-sync"},
                                {"device": "lbnl-data-sw", "result": "in-sync"},
                            ]
                        },
                    },
                    "hardware_health": {
                        "renc-data-sw": {"ok": True},
                        "lbnl-data-sw": {"ok": True},
                    },
                    "physical_operational_edges": [],
                },
            },
        },
    )
    return case


def test_device_health_section_in_report():
    case = _case_with_spines()

    topo = topology_from_case(case)
    assert topo["operational"]["layers"]["underlay"]["edges"]
    assert topo["operational"]["layers"]["routing"]["edges"]

    section = "\n".join(format_device_health_section(case))
    assert "Device" in section
    assert "Sync" in section
    assert "renc-data-sw" in section
    assert "lbnl-data-sw" in section
    assert "in-sync" in section

    text = render_report(case)
    assert "## Devices" in text
    assert "### renc-data-sw" in text
    assert "### lbnl-data-sw" in text
    assert "In sync" in text
    assert "Detailed Device Analysis" not in text


def test_endpoints_shaped_edges_normalized():
    case = CaseFile(
        budget=Budget(max_deep_checks=0, max_handoffs=0),
        device_names=["a-sw", "b-sw"],
    )
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "isis",
            "layer": "underlay",
            "payload": {
                "operational_edges": [
                    {
                        "id": "u1",
                        "endpoints": [
                            {"device": "a-sw", "interface": "Hu0/0/0/0"},
                            {"device": "b-sw", "interface": "Hu0/0/0/1"},
                        ],
                        "state": {"status": "up"},
                    }
                ],
            },
        },
    )
    topo = topology_from_case(case)
    edge = topo["operational"]["layers"]["underlay"]["edges"][0]
    assert edge["local"]["device"] == "a-sw"
    assert edge["remote"]["device"] == "b-sw"


def test_routing_count_prose_unknown_is_na():
    from diagnostic_mas.operator_report import _routing_count_prose

    assert _routing_count_prose("—") == "N/A"
    assert _routing_count_prose("") == "N/A"
    assert _routing_count_prose("2/2") == "2/2 up"
    assert _routing_count_prose("0/2") == "0/2 up"


def test_bgp_routing_qualification_spine_vs_drill():
    """0/2 from incomplete peer live checks; drill Established on reachable side."""
    from diagnostic_mas.case import Budget, CaseFile, add_evidence
    from diagnostic_mas.operator_report import (
        _bgp_routing_qualification,
        format_devices_operator,
    )
    from diagnostic_mas.device_health import topology_from_case
    from nso_report.executive import build_device_health_rows

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    case.device_names = ["atla-data-sw", "star-data-sw", "wash-data-sw"]
    case.focus_devices = ["atla-data-sw"]
    case.issues = [
        {
            "id": "is_64",
            "code": "configured_no_session",
            "status": "open",
            "edge_id": "bgp:10.129.128.1:10.138.128.1:star-data-sw:atla-data-sw",
            "message": (
                "Configured/static BGP session missing live Established state: "
                "star-data-sw 10.129.128.1 ↔ atla-data-sw 10.138.128.1"
            ),
            "layer": "routing",
        },
        {
            "id": "is_65",
            "code": "configured_no_session",
            "status": "open",
            "edge_id": "bgp:10.133.0.1:10.138.128.1:wash-data-sw:atla-data-sw",
            "message": (
                "Configured/static BGP session missing live Established state: "
                "wash-data-sw 10.133.0.1 ↔ atla-data-sw 10.138.128.1"
            ),
            "layer": "routing",
        },
    ]
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "bgp",
            "layer": "routing",
            "payload": {
                "static_edges": [
                    {
                        "id": "bgp:10.129.128.1:10.138.128.1:star-data-sw:atla-data-sw",
                        "local": {"device": "star-data-sw", "address": "10.129.128.1"},
                        "remote": {"device": "atla-data-sw", "address": "10.138.128.1"},
                    },
                    {
                        "id": "bgp:10.133.0.1:10.138.128.1:wash-data-sw:atla-data-sw",
                        "local": {"device": "wash-data-sw", "address": "10.133.0.1"},
                        "remote": {"device": "atla-data-sw", "address": "10.138.128.1"},
                    },
                ],
                "operational_edges": [
                    {
                        "id": "bgp:10.129.128.1:10.138.128.1:star-data-sw:atla-data-sw",
                        "state": {
                            "status": "down",
                            "detail": "no operational BGP session observed",
                        },
                    },
                    {
                        "id": "bgp:10.133.0.1:10.138.128.1:wash-data-sw:atla-data-sw",
                        "state": {
                            "status": "down",
                            "detail": "no operational BGP session observed",
                        },
                    },
                ],
            },
        },
    )
    add_evidence(
        case,
        {
            "kind": "drill_finding",
            "role": "drill",
            "layer": "routing",
            "payload": {
                "issue_id": "is_64",
                "issue_edge_id": (
                    "bgp:10.129.128.1:10.138.128.1:star-data-sw:atla-data-sw"
                ),
                "observed": "star quarantined; facts from atla only",
                "cause": (
                    "No real BGP fault on the reachable side: session is "
                    "Established on atla-data-sw"
                ),
                "session_state": "established",
            },
        },
    )
    add_evidence(
        case,
        {
            "kind": "drill_finding",
            "role": "drill",
            "layer": "routing",
            "payload": {
                "issue_id": "is_65",
                "issue_edge_id": (
                    "bgp:10.133.0.1:10.138.128.1:wash-data-sw:atla-data-sw"
                ),
                "observed": "wash quarantined",
                "cause": (
                    "Monitoring/telemetry false positive; session IS Established"
                ),
                "session_state": "established",
            },
        },
    )

    bgp_prose, drill = _bgp_routing_qualification(case, "atla-data-sw", "0/2")
    assert bgp_prose == "0/2 up (initial; peer live checks unavailable)"
    assert drill is not None
    assert "reachable side only" in drill
    assert "`star-data-sw`" in drill
    assert "`wash-data-sw`" in drill
    assert "`atla-data-sw`" in drill

    # Without drill findings, still qualify incomplete spine check.
    case2 = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    case2.issues = list(case.issues)
    prose2, drill2 = _bgp_routing_qualification(case2, "atla-data-sw", "0/2")
    assert prose2 == "0/2 up (initial; peer live checks unavailable)"
    assert drill2 is None

    text = "\n".join(format_devices_operator(case))
    assert "### atla-data-sw" in text
    assert "BGP 0/2 up (initial; peer live checks unavailable)" in text
    assert "**Drill:** BGP Established on `atla-data-sw`" in text
    assert "reachable side only" in text



def test_drill_line_names_the_side_that_was_read_when_the_device_itself_was_not():
    """brist could not be read; the drill's Established reading came from wash."""
    from diagnostic_mas.operator_report import _bgp_routing_qualification

    edge = "bgp:10.133.0.1:10.144.0.1:wash-data-sw:brist-data-sw"
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    case.device_names = ["brist-data-sw", "wash-data-sw"]
    case.live_verified_devices = ["wash-data-sw"]
    case.issues = [{
        "id": "is_7", "code": "configured_no_session", "status": "open", "edge_id": edge,
        "layer": "routing",
        "message": "BGP wash-data-sw 10.133.0.1 (idle) ↔ brist-data-sw 10.144.0.1 (unknown)",
    }]
    add_evidence(case, {"kind": "drill_finding", "role": "drill", "layer": "routing", "payload": {
        "issue_id": "is_7", "issue_edge_id": edge, "observed": "brist-data-sw is southbound locked",
        "cause": "The idle state is a stale snapshot; the session is Established now on wash-data-sw.",
        "session_state": "established"}})

    _, unread = _bgp_routing_qualification(case, "brist-data-sw", "0/1")
    _, read = _bgp_routing_qualification(case, "wash-data-sw", "0/1")

    assert unread == ("**Drill:** `wash-data-sw` reports its BGP session with `brist-data-sw` Established; "
                      "`brist-data-sw` itself could not be read this run")
    assert read == ("**Drill:** BGP Established on `wash-data-sw` toward `brist-data-sw` "
                    "(reachable side only; peer devices not live-checked this run)")


def test_drill_line_follows_the_reported_session_state_not_words_in_the_text():
    """As test46's star-cape drill wrote it: quarantined peer, other peers Established, this one Idle."""
    from diagnostic_mas.operator_report import _bgp_routing_qualification

    edge = "bgp:10.129.128.1:10.148.128.1:star-data-sw:cape-data-sw"

    def case_with(**extra):
        case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
        case.device_names = ["cape-data-sw", "star-data-sw"]
        case.live_verified_devices = ["star-data-sw"]
        case.issues = [{"id": "is_34", "code": "configured_no_session", "status": "open", "edge_id": edge,
                        "layer": "routing", "message": "BGP star-data-sw 10.129.128.1 (idle) <-> cape-data-sw"}]
        add_evidence(case, {"kind": "drill_finding", "role": "drill", "layer": "routing", "payload": {
            "issue_id": "is_34", "issue_edge_id": edge,
            "observed": ("cape-data-sw is quarantined/unavailable. Neighbor 10.148.128.1 is Idle; all other "
                         "iBGP peers are Established. Connections established 5 / dropped 5."),
            "cause": "No route to multi-hop neighbor; the session is held in Idle.", **extra}})
        return case

    for extra in ({"session_state": "not_established"}, {"session_state": "unknown"}, {}):
        for device in ("cape-data-sw", "star-data-sw"):
            assert _bgp_routing_qualification(case_with(**extra), device, "0/1")[1] is None, (extra, device)

def test_collection_status_reason_device_timeout():
    from diagnostic_mas.operator_report import _collection_status_reason

    reason = _collection_status_reason(
        {
            "status": "unknown",
            "system_status": "unknown",
            "in_sync": None,
            "device_sync": {
                "atla-data-sw": "in-sync",
                "star-data-sw": (
                    "error: HTTPSConnectionPool(host='192.0.2.2', port=443): "
                    "Read timed out. (read timeout=10)"
                ),
            },
        }
    )
    assert reason is not None
    assert "star-data-sw" in reason
    assert "timed out" in reason.lower()
    assert "HTTPSConnectionPool" not in reason


def test_collection_status_reason_out_of_sync():
    from diagnostic_mas.operator_report import _collection_status_reason

    reason = _collection_status_reason(
        {
            "status": "degraded",
            "in_sync": False,
            "device_sync": {"renc-data-sw": "out-of-sync"},
        }
    )
    assert reason is not None
    assert "out-of-sync" in reason


def test_collection_status_reason_none_when_up():
    from diagnostic_mas.operator_report import _collection_status_reason

    assert (
        _collection_status_reason(
            {
                "status": "up",
                "device_sync": {"a": "in-sync", "b": "in-sync"},
            }
        )
        is None
    )


def test_collection_result_includes_reason():
    from diagnostic_mas.case import Budget, CaseFile, add_evidence
    from diagnostic_mas.operator_report import format_services_operator

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "extra": {
                    "services": {
                        "l3rt/svc-1": {
                            "name": "svc-1",
                            "service_type": "l3rt",
                            "status": "unknown",
                            "system_status": "unknown",
                            "dataplane_status": "not_checked",
                            "device_sync": {
                                "star-data-sw": (
                                    "error: Read timed out. (read timeout=10)"
                                ),
                            },
                        }
                    }
                }
            },
        },
    )
    text = "\n".join(format_services_operator(case, services_detail=True))
    assert "Collection verification incomplete" in text
    assert "not a confirmed forwarding fault" in text
    assert "star-data-sw" in text
    assert "timed out" in text.lower()
    assert "Basic checks passed" not in text
    assert "collection verification incomplete" in text.lower()
    # Even if coverage map wrongly says basic_passed:
    case.service_coverage = {"svc-1": "basic_passed"}
    text2 = "\n".join(format_services_operator(case, services_detail=True))
    assert "Basic checks passed" not in text2
    assert "not a confirmed forwarding fault" in text2


def test_health_prose_treats_healthy_label_as_ok():
    from diagnostic_mas.operator_report import _health_prose

    prose = _health_prose(
        {"interfaces": "Healthy", "hardware": "Healthy", "notes": ""}
    )
    assert prose == "Interface and hardware checks reported healthy"
    assert "confirmed hardware fault" not in prose


def test_health_prose_states_observations_not_fault():
    from diagnostic_mas.operator_report import _health_prose

    row = {
        "interfaces": "Inventory Review",
        "hardware": "Review",
        "notes": "Mapping unknown",
    }
    hw = {
        "temperature": [{"ok": True}],
        "fans": [{"ok": True}],
        "power": [{"ok": True}],
        "control_plane": [{"dropped": 123}],
    }
    lean = _health_prose(row, hardware_entry=hw)
    assert "NSO↔device interface mapping unconfirmed" in lean
    assert "control-plane drop counter=123 recorded" in lean
    assert "confirmed hardware fault" in lean
    assert "per-device details are in the HTML report" in lean
    assert "--full" not in lean
    assert "require explanation" not in lean


def test_health_prose_still_flags_real_review():
    from diagnostic_mas.operator_report import _health_prose

    prose = _health_prose(
        {
            "interfaces": "Inventory Review",
            "hardware": "Review",
            "notes": "Mapping unknown — see Detailed Analysis",
        },
    )
    assert "Inventory Review" in prose
    assert "mapping unconfirmed" in prose
    assert "per-device details are in the HTML report" in prose


def test_health_prose_says_what_was_not_read():
    """cien-data-sw in test47: interfaces read and healthy, hardware not readable on a Ciena."""
    from diagnostic_mas.operator_report import _health_prose

    def prose(interfaces, hardware):
        return _health_prose({"interfaces": interfaces, "hardware": hardware, "notes": ""})

    assert prose("Healthy", "Healthy") == "Interface and hardware checks reported healthy"
    assert prose("Healthy", "Unavailable") == "Interface checks reported healthy; hardware status not collected"
    assert prose("—", "Healthy") == "Hardware checks reported healthy; interface status not collected"
    assert prose("—", "Unavailable") == "Interface and hardware status not collected for this run"


def test_drill_line_does_not_count_a_device_as_read_when_its_bgp_collection_failed():
    """cien answers get_interface_health, so it is live-verified, but its BGP cannot be read."""
    from diagnostic_mas.operator_report import _bgp_routing_qualification

    edge = "bgp:10.129.128.1:10.148.0.1:star-data-sw:cien-data-sw"
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    case.device_names = ["cien-data-sw", "star-data-sw"]
    case.live_verified_devices = ["cien-data-sw", "star-data-sw"]
    case.issues = [
        {"id": "is_9", "code": "missing_reverse_session", "status": "open", "edge_id": edge, "layer": "routing",
         "message": "star-data-sw sees 10.148.0.1 (Established) but cien-data-sw has no matching reciprocal row"},
        {"code": "collection_error", "status": "open", "layer": "routing", "edge_id": None,
         "message": "cien-data-sw: BGP summary collection failed: device 'cien-data-sw' exists but has no CLI exec action"},
    ]
    add_evidence(case, {"kind": "drill_finding", "role": "drill", "layer": "routing", "payload": {
        "issue_id": "is_9", "issue_edge_id": edge, "observed": "star: neighbor 10.148.0.1 Established, 5d",
        "cause": "Session up from star; cien could not be read.", "session_state": "established"}})

    assert _bgp_routing_qualification(case, "cien-data-sw", "0/1")[1] == (
        "**Drill:** `star-data-sw` reports its BGP session with `cien-data-sw` Established; "
        "`cien-data-sw` itself could not be read this run")
    assert _bgp_routing_qualification(case, "star-data-sw", "0/1")[1] == (
        "**Drill:** BGP Established on `star-data-sw` toward `cien-data-sw` "
        "(reachable side only; peer devices not live-checked this run)")


def _routing_case():
    """test51's readings: star, dall and newy answered; brist, prin, cien and cape could not be read."""
    from diagnostic_mas.case import Budget, CaseFile, add_evidence

    def bgp(a, b, la, lb, status):
        edge_id = f"bgp:10.0.0.1:10.0.0.2:{a}:{b}"
        return ({"id": edge_id, "local": {"device": a}, "remote": {"device": b}},
                {"id": edge_id, "state": {"local": la, "remote": lb, "status": status}})

    def isis(a, if_a, b, if_b, la, lb, status):
        edge_id = f"isis:{a}:{if_a}:{b}:{if_b}"
        return ({"id": edge_id, "local": {"device": a, "interface": if_a},
                 "remote": {"device": b, "interface": if_b}},
                {"id": edge_id, "state": {"local": la, "remote": lb, "status": status}})

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    sessions = [
        bgp("star-data-sw", "wash-data-sw", "established", "established", "up"),
        bgp("star-data-sw", "prin-data-sw", "established", "unknown", "unknown"),
        bgp("star-data-sw", "brist-data-sw", "established", "unknown", "unknown"),
        bgp("star-data-sw", "cien-data-sw", "established", "unknown", "unknown"),
        bgp("star-data-sw", "cape-data-sw", "idle", "unknown", "down"),
        bgp("dall-data-sw", "cien-data-sw", "idle", "unknown", "down"),
    ]
    adjacencies = [
        isis("newy-data-sw", "Bundle-Ether101.3001", "star-data-sw", "Bundle-Ether101.3001", "up", "up", "up"),
        isis("newy-data-sw", "HundredGigE0/0/0/20.3000", "prin-data-sw", "?", "up", "unknown", "unknown"),
    ]
    for role, layer, edges in (("bgp", "routing", sessions), ("isis", "underlay", adjacencies)):
        add_evidence(case, {"kind": "spine", "role": role, "layer": layer, "payload": {
            "static_edges": [s for s, _ in edges], "operational_edges": [o for _, o in edges]}})
    return case


def test_routing_lines_show_what_each_end_read():
    from diagnostic_mas.operator_report import format_devices_operator

    text = "\n".join(format_devices_operator(_routing_case()))
    lines = {}
    device = None
    for line in text.splitlines():
        if line.startswith("### "):
            device = line[4:]
        elif line.startswith("**Routing:**"):
            lines[device] = line

    assert lines["star-data-sw"] == (
        "**Routing:** BGP 1/5 up; 3 Established here, far end not read "
        "(brist-data-sw, cien-data-sw, prin-data-sw); 1 Idle (cape-data-sw) · IS-IS 1/1 up")
    assert lines["prin-data-sw"] == (
        "**Routing:** BGP not read on prin-data-sw; its peers report 1 of 1 Established (star-data-sw)"
        " · IS-IS not read on prin-data-sw; its neighbors report 1 of 1 up (newy-data-sw)")
    assert lines["cien-data-sw"].startswith(
        "**Routing:** BGP not read on cien-data-sw; its peers report 1 of 2 Established (star-data-sw), "
        "1 Idle (dall-data-sw) · ")
    assert lines["cape-data-sw"].startswith(
        "**Routing:** BGP not read on cape-data-sw; its peers report 0 of 1 Established, 1 Idle (star-data-sw) · ")
    assert lines["dall-data-sw"].startswith("**Routing:** BGP 0/1 up; 1 Idle (cien-data-sw) · ")
    assert lines["newy-data-sw"] == (
        "**Routing:** BGP N/A · IS-IS 1/2 up; 1 up here, far end not read (prin-data-sw)")
    # Sessions read from both ends keep the plain count.
    assert lines["wash-data-sw"] == "**Routing:** BGP 1/1 up · IS-IS N/A"
