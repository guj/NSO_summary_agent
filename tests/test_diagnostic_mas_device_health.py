from diagnostic_mas.case import Budget, CaseFile, add_evidence
from diagnostic_mas.device_health import (
    format_detailed_devices_section,
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


def test_detailed_device_analysis_in_report():
    case = _case_with_spines()
    detail = "\n".join(format_detailed_devices_section(case))
    assert "Device: renc-data-sw" in detail
    assert "Device: lbnl-data-sw" in detail
    assert "Status" in detail
    assert "Routing" in detail

    text = render_report(case, full=True)
    assert "## Appendix: Detailed Device Analysis" in text
    assert text.index("## Devices") < text.index("## Appendix: Detailed Device Analysis")
    assert text.index("## Run details") < text.index("## Appendix: Detailed Device Analysis")
    assert "Device: renc-data-sw" in text
    # Detail is the last major section
    assert text.rfind("## Appendix: Detailed Device Analysis") == text.rfind("##")


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
    detail = "\n".join(format_detailed_devices_section(case))
    assert "Device: a-sw" in detail
    assert "Device: b-sw" in detail


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
                    "error: HTTPSConnectionPool(host='192.168.11.246', port=443): "
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
    lean = _health_prose(row, full=False, hardware_entry=hw)
    assert "NSO↔device interface mapping unconfirmed" in lean
    assert "control-plane drop counter=123 recorded" in lean
    assert "confirmed hardware fault" in lean
    assert "use --full" in lean
    assert "require explanation" not in lean
    full = _health_prose(row, full=True, hardware_entry=hw)
    assert "see Detailed Analysis" in full
    assert "use --full" not in full


def test_health_prose_still_flags_real_review():
    from diagnostic_mas.operator_report import _health_prose

    prose = _health_prose(
        {
            "interfaces": "Inventory Review",
            "hardware": "Review",
            "notes": "Mapping unknown — see Detailed Analysis",
        },
        full=False,
    )
    assert "Inventory Review" in prose
    assert "mapping unconfirmed" in prose
    assert "use --full" in prose
