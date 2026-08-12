from agent.report_devices import format_devices_section


def _topo(physical, underlay, routing, *, issues=None, route_summary=None):
    operational = {
        "layers": {
            "physical": {"edges": physical, "summary": {}},
            "underlay": {"edges": underlay, "summary": {}},
            "routing": {"edges": routing, "summary": {}},
        }
    }
    if issues is not None:
        operational["issues"] = issues
    if route_summary is not None:
        operational["route_summary"] = route_summary
    return {"operational": operational}


def test_devices_banner_and_healthy_overall():
    topology = _topo(
        [
            {
                "id": "if:sw1:Loopback0",
                "local": {"device": "sw1", "interface": "Loopback0"},
                "state": {"admin": "up", "oper": "up", "status": "up"},
            }
        ],
        [
            {
                "id": "isis:a",
                "local": {"device": "sw1", "interface": "Hu0/0/0/0"},
                "remote": {"device": "sw2", "interface": "Hu0/0/0/1"},
                "state": {"status": "up"},
            }
        ],
        [
            {
                "id": "bgp:a",
                "local": {"device": "sw1", "address": "10.0.0.1"},
                "remote": {"device": "sw2", "address": "10.0.0.2"},
                "state": {"status": "up"},
            }
        ],
    )
    text = format_devices_section(
        topology,
        fleet_sync={
            "status": "success",
            "data": {"devices": [{"device": "sw1", "result": "in-sync"}]},
        },
    )
    assert "Device: sw1" in text
    assert "Overall: Healthy" in text
    assert "Device is synchronized with NSO" in text
    assert "BGP and IS-IS are healthy" in text
    assert "Observations" not in text
    assert "Exceptions" not in text
    assert "Peers: 1" in text
    assert "10.0.0.2" in text and "Established" in text
    assert "Up/Up:" in text


def test_unexpected_live_in_exceptions_not_interface_health():
    topology = _topo(
        [
            {
                "id": "if:sw1:Loopback0",
                "local": {"device": "sw1", "interface": "Loopback0"},
                "state": {"admin": "up", "oper": "up", "status": "up"},
            }
        ],
        [],
        [],
        issues=[
            {
                "code": "unexpected_live_object",
                "layer": "physical",
                "severity": "low",
                "edge_id": "if:sw1:Nu0",
                "message": "sw1 Nu0: live interface not present in static config",
            },
            {
                "code": "unexpected_live_object",
                "layer": "physical",
                "edge_id": "if:sw1:Mg0/RP0/CPU0/0",
                "message": (
                    "sw1 Mg0/RP0/CPU0/0: live interface not present in static config"
                ),
            },
        ],
    )
    text = format_devices_section(
        topology,
        fleet_sync={
            "status": "success",
            "data": {"devices": [{"device": "sw1", "result": "in-sync"}]},
        },
    )
    assert "Overall: Healthy" in text
    assert "Mapping Unknown: 0" in text
    assert "Live interface not in static config:" in text
    assert "Nu0" in text
    assert "Mg0/RP0/CPU0/0" in text
    # Not counted in configured-interface summary total
    assert "Total:       1" in text


def test_devices_review_with_inventory_and_operational_down():
    topology = _topo(
        [
            {
                "id": "if:sw1:a",
                "local": {"device": "sw1", "interface": "a"},
                "state": {"admin": "up", "oper": "up", "status": "up"},
            },
            {
                "id": "if:sw1:b",
                "local": {"device": "sw1", "interface": "Hu0/0/0/3"},
                "state": {"admin": "up", "oper": "down", "status": "down"},
            },
            {
                "id": "if:sw1:c",
                "local": {"device": "sw1", "interface": "c"},
                "state": {
                    "admin": "admin-down",
                    "oper": "admin-down",
                    "status": "admin-down",
                },
            },
            {
                "id": "if:sw1:u",
                "local": {"device": "sw1", "interface": "Fo0/0/0/32"},
                "state": {"admin": "unknown", "oper": "unknown"},
            },
        ],
        [],
        [
            {
                "id": "bgp:a",
                "local": {"device": "sw1", "address": "1.1.1.1"},
                "remote": {"device": "sw2", "address": "2.2.2.2"},
                "state": {"status": "up"},
            },
            {
                "id": "bgp:b",
                "local": {"device": "sw1", "address": "1.1.1.1"},
                "remote": {"device": "sw2", "address": "3.3.3.3"},
                "state": {"status": "degraded"},
            },
        ],
        issues=[
            {
                "code": "config_live_mismatch",
                "edge_id": "if:sw1:u",
                "device": "sw1",
                "nso": "Fo0/0/0/32",
                "kind": "type_change",
                "confirmed": False,
                "candidates": ["Hu0/0/0/32"],
            },
            {
                "code": "unknown_neighbor_address",
                "message": (
                    "sw1 10.148.0.1: could not map neighbor address to NSO device"
                ),
            },
        ],
        route_summary={"sw1": {"total": 446595, "sources": {"bgp 398900": 1}}},
    )
    text = format_devices_section(topology)
    assert "Overall: Inventory Review" in text
    assert "interface inventory mismatch" in text
    assert "Routes" in text
    assert "446,595" in text
    assert "bgp 398900: 1" in text
    assert "Exceptions" in text
    assert "Inventory mismatch:" in text
    assert "Fo0/0/0/32 → Hu0/0/0/32  [suggested]" in text
    assert "Interface up/down:" in text
    assert "Hu0/0/0/3:" in text
    assert "BGP peer not mapped to inventory:" in text
    assert "10.148.0.1" in text
    # Admin-down names not listed under Exceptions
    assert text.index("Admin Down:") < text.index("Exceptions")


def test_admin_down_not_listed_under_exceptions():
    edges = [
        {
            "id": f"if:sw1:{i}",
            "local": {"device": "sw1", "interface": f"GigabitEthernet0/0/0/{i}"},
            "state": {
                "admin": "admin-down",
                "oper": "admin-down",
                "status": "admin-down",
            },
        }
        for i in range(3)
    ]
    text = format_devices_section(_topo(edges, [], []))
    assert "Exceptions" not in text
    assert "Overall: Healthy" in text
    assert "GigabitEthernet0/0/0/0" not in text


def test_interface_down_down_names_four_per_row():
    edges = [
        {
            "id": f"if:sw1:{i}",
            "local": {"device": "sw1", "interface": f"HundredGigE0/0/0/{i}"},
            "state": {"admin": "down", "oper": "down"},
        }
        for i in range(5)
    ]
    text = format_devices_section(_topo(edges, [], []))
    assert "Interface down/down:" in text
    assert "Interface up/down:" not in text
    block = text.split("Interface down/down:", 1)[1]
    name_lines = [
        ln for ln in block.splitlines() if "HundredGigE" in ln
    ]
    assert len(name_lines) >= 2
    assert name_lines[0].count("HundredGigE") == 4
    assert name_lines[1].count("HundredGigE") == 1


def test_interface_up_down_one_row_with_investigation():
    topology = _topo(
        [
            {
                "id": "if:sw1:Hu",
                "local": {"device": "sw1", "interface": "HundredGigE0/0/0/3"},
                "state": {"admin": "up", "oper": "down"},
            },
            {
                "id": "if:sw1:Hu4",
                "local": {"device": "sw1", "interface": "HundredGigE0/0/0/4"},
                "state": {"admin": "up", "oper": "down"},
            },
        ],
        [],
        [],
    )
    topology["operational"]["interface_troubleshooting"] = {
        "sw1": {
            "interface": "HundredGigE0/0/0/3",
            "reason": "up_down",
            "summary": "L1 carrier loss",
        }
    }
    text = format_devices_section(topology)
    assert "Interface up/down:" in text
    assert "HundredGigE0/0/0/3: L1 carrier loss" in text
    assert "HundredGigE0/0/0/4: (not investigated)" in text
    assert "Interface down/down:" not in text


def test_services_per_device_table():
    topology = _topo(
        [
            {
                "id": "if:sw1:Lo0",
                "local": {"device": "sw1", "interface": "Loopback0"},
                "state": {"admin": "up", "oper": "up"},
            }
        ],
        [],
        [],
    )
    services = {
        "l2ptp/a": {
            "service_type": "l2ptp",
            "name": "a",
            "devices": ["sw1", "sw2"],
            "status": "up",
        },
        "l2bridge/b": {
            "service_type": "l2bridge",
            "name": "b",
            "devices": ["sw1"],
            "status": "up",
        },
    }
    text = format_devices_section(topology, services=services)
    assert "Deployed Services: 2" in text
    assert "l2ptp" in text
    assert "l2bridge" in text
    assert "Services are operational" in text


def test_suggested_equivalences_preamble_still_present():
    topology = _topo(
        [
            {
                "id": "if:renc:Fo32",
                "local": {"device": "renc-data-sw", "interface": "FourHundredGigE0/0/0/32"},
                "state": {"admin": "unknown", "oper": "unknown"},
            }
        ],
        [],
        [],
        issues=[
            {
                "code": "config_live_mismatch",
                "edge_id": "if:renc:Fo32",
                "device": "renc-data-sw",
                "kind": "type_change",
                "confirmed": False,
                "candidates": ["Hu0/0/0/32"],
            }
        ],
    )
    text = format_devices_section(topology)
    assert "unconfirmed NSO↔box" in text or "interface-equivalence" in text
    assert "Device: renc-data-sw" in text
    assert "Inventory mismatch:" in text


def test_devices_joins_static_identity_with_operational_state():
    topology = {
        "static": {
            "layers": {
                "physical": {
                    "edges": [
                        {
                            "id": "if:sw1:Hu0",
                            "local": {
                                "device": "sw1",
                                "interface": "HundredGigE0/0/0/0",
                            },
                        }
                    ]
                },
                "underlay": {"edges": []},
                "routing": {
                    "edges": [
                        {
                            "id": "bgp:1",
                            "local": {"device": "sw1", "address": "1.1.1.1"},
                            "remote": {"device": "sw2", "address": "2.2.2.2"},
                        }
                    ]
                },
            }
        },
        "operational": {
            "layers": {
                "physical": {
                    "edges": [
                        {
                            "id": "if:sw1:Hu0",
                            "state": {"admin": "up", "oper": "up", "status": "up"},
                        }
                    ]
                },
                "underlay": {"edges": []},
                "routing": {
                    "edges": [{"id": "bgp:1", "state": {"status": "up"}}]
                },
            }
        },
    }
    text = format_devices_section(topology)
    assert "Device: sw1" in text
    assert "Peers: 1" in text
    assert "2.2.2.2" in text


def test_devices_lists_unmapped_isis():
    topology = _topo(
        [
            {
                "id": "if:sw1:Lo0",
                "local": {"device": "sw1", "interface": "Loopback0"},
                "state": {"admin": "up", "oper": "up"},
            }
        ],
        [],
        [],
        issues=[
            {
                "code": "unknown_neighbor_system_id",
                "message": (
                    "sw1 Hu0/0/0/0: unmapped IS-IS system id 'star-data-sw'"
                ),
            }
        ],
    )
    text = format_devices_section(topology)
    assert "Unmapped IS-IS adjacency:" in text
    assert "star-data-sw" in text
