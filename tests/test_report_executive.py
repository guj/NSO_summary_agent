from nso_report.executive import (
    build_device_health_rows,
    device_interfaces_label,
)


def test_interfaces_review_on_up_down():
    phys = [
        {
            "id": "if:sw1:a",
            "local": {"device": "sw1", "interface": "a"},
            "state": {"admin": "up", "oper": "down"},
        }
    ]
    assert device_interfaces_label("sw1", phys, {}) == "Inventory Review"


def test_interfaces_healthy_when_only_admin_down():
    phys = [
        {
            "id": "if:sw1:a",
            "local": {"device": "sw1", "interface": "a"},
            "state": {
                "admin": "admin-down",
                "oper": "admin-down",
            },
        }
    ]
    assert device_interfaces_label("sw1", phys, {}) == "Healthy"


def test_build_health_rows():
    topology = {
        "static": {
            "layers": {
                "physical": {
                    "edges": [
                        {
                            "id": "if:sw1:Lo0",
                            "local": {"device": "sw1", "interface": "Loopback0"},
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
                            "id": "if:sw1:Lo0",
                            "state": {"admin": "up", "oper": "up", "status": "up"},
                        }
                    ]
                },
                "underlay": {"edges": []},
                "routing": {
                    "edges": [{"id": "bgp:1", "state": {"status": "up"}}]
                },
            },
        },
    }
    rows = build_device_health_rows(topology)
    assert rows and rows[0]["device"] == "sw1"
    assert rows[0]["interfaces"] == "Healthy"
    assert rows[0]["bgp"] == "1/1"
    assert rows[0]["sync"] == "—"


def test_device_health_notes_skip_admin_down_and_down_down():
    from nso_report.executive import device_notes_label

    phys = [
        {
            "id": "if:lbnl:a",
            "local": {"device": "lbnl-data-sw", "interface": "Hu0/0/0/3"},
            "state": {"admin": "down", "oper": "down"},
        },
        {
            "id": "if:lbnl:b",
            "local": {"device": "lbnl-data-sw", "interface": "Hu0/0/0/4"},
            "state": {"admin": "admin-down", "oper": "admin-down"},
        },
        {
            "id": "if:uky:u",
            "local": {"device": "uky-data-sw", "interface": "Fo0/0/0/34"},
            "state": {"admin": "unknown", "oper": "unknown"},
        },
    ]
    assert device_notes_label("lbnl-data-sw", phys, {}) == ""
    assert device_notes_label("uky-data-sw", phys, {}) == "Mapping unknown"


def test_device_health_sync_column():
    from nso_report.executive import build_device_health_rows

    topology = {
        "static": {
            "layers": {
                "physical": {
                    "edges": [
                        {
                            "id": "if:sw1:Lo0",
                            "local": {"device": "sw1", "interface": "Loopback0"},
                        }
                    ]
                },
                "underlay": {"edges": []},
                "routing": {"edges": []},
            }
        },
        "operational": {
            "layers": {
                "physical": {
                    "edges": [
                        {
                            "id": "if:sw1:Lo0",
                            "state": {"admin": "up", "oper": "up", "status": "up"},
                        }
                    ]
                },
                "underlay": {"edges": []},
                "routing": {"edges": []},
            },
        },
    }
    fleet = {
        "status": "success",
        "data": {
            "devices": [
                {"device": "sw1", "result": "out-of-sync"},
            ]
        },
    }
    rows = build_device_health_rows(topology, fleet)
    assert rows[0]["sync"] == "out-of-sync"
    assert rows[0]["hardware"] == "Unavailable"


def test_device_health_hardware_column():
    from nso_report.executive import build_device_health_rows

    topology = {
        "static": {
            "layers": {
                "physical": {
                    "edges": [
                        {
                            "id": "if:sw1:Lo0",
                            "local": {"device": "sw1", "interface": "Loopback0"},
                        }
                    ]
                },
                "underlay": {"edges": []},
                "routing": {"edges": []},
            }
        },
        "operational": {
            "layers": {
                "physical": {
                    "edges": [
                        {
                            "id": "if:sw1:Lo0",
                            "state": {"admin": "up", "oper": "up", "status": "up"},
                        }
                    ]
                },
                "underlay": {"edges": []},
                "routing": {"edges": []},
            },
        },
    }
    hw = {
        "sw1": {
            "fans": [{"ok": True}],
            "power": [{"ok": True}],
            "control_plane": [{"dropped": 3}],
        }
    }
    rows = build_device_health_rows(topology, hardware_health=hw)
    assert rows[0]["hardware"] == "Review"


def test_port_the_device_gives_no_state_for_is_not_a_mapping_problem():
    """As cien-data-sw's management ports m1-m4: on the device by name, but no state reported."""
    from nso_report.executive import device_notes_label
    from nso_report.devices import device_detail_lines

    phys = [
        {"id": "if:cien-data-sw:14", "local": {"device": "cien-data-sw", "interface": "14"},
         "state": {"admin": "up", "oper": "up", "status": "up"}},
        {"id": "if:cien-data-sw:1", "local": {"device": "cien-data-sw", "interface": "1"},
         "state": {"admin": "down", "oper": "down", "status": "down"}},
        {"id": "if:cien-data-sw:m1", "local": {"device": "cien-data-sw", "interface": "m1"},
         "state": {"status": "unknown", "detail": "no interface state from get_interface_health", "no_state": True}},
        # An IOS-XR name NSO has but the device's interfaces brief lacks: still a mapping problem.
        {"id": "if:uky-data-sw:Fo0/0/0/34", "local": {"device": "uky-data-sw", "interface": "Fo0/0/0/34"},
         "state": {"status": "unknown", "detail": "not in interfaces brief"}},
    ]
    assert device_interfaces_label("cien-data-sw", phys, {}) == "Healthy"
    assert device_notes_label("cien-data-sw", phys, {}) == ""
    assert device_interfaces_label("uky-data-sw", phys, {}) == "Inventory Review"
    assert device_notes_label("uky-data-sw", phys, {}) == "Mapping unknown"

    topology = {"operational": {"layers": {"physical": {"edges": phys}, "underlay": {"edges": []},
                                           "routing": {"edges": []}}}}
    cien = "\n".join(device_detail_lines(topology)["cien-data-sw"])
    assert "not on box" not in cien and "Mapping Unknown: 0" in cien and "No state:    1" in cien
    assert "Fo0/0/0/34 → (not on box)" in "\n".join(device_detail_lines(topology)["uky-data-sw"])


def test_device_whose_interfaces_were_not_read_is_not_a_mapping_problem():
    """As cape-data-sw (NSO could not connect) and brist-data-sw (southbound locked)."""
    from nso_report.executive import device_notes_label
    from diagnostic_mas.operator_report import _health_prose
    from nso_facts.fleet_rollups import device_needs_inventory_review
    from nso_report.devices import device_detail_lines

    def edge(device, name, state):
        return {"id": f"if:{device}:{name}", "local": {"device": device, "interface": name}, "state": state}

    unread = {"status": "unknown", "detail": "Device brist-data-sw is southbound locked", "not_read": True}
    no_state = {"status": "unknown", "detail": "no interface state from get_interface_health", "no_state": True}
    phys = [edge("brist-data-sw", "Hu0/0/0/1", unread), edge("brist-data-sw", "Hu0/0/0/2", unread),
            edge("cien-data-sw", "14", {"admin": "up", "oper": "up", "status": "up"}),
            edge("cien-data-sw", "m1", no_state)]

    assert device_interfaces_label("brist-data-sw", phys, {}) == "—"
    assert device_notes_label("brist-data-sw", phys, {}) == ""
    assert (_health_prose({"interfaces": "—", "hardware": "Unavailable", "notes": ""})
            == "Interface and hardware status not collected for this run")
    # The fleet count behind Grafana agrees with the report, for unread and stateless ports alike.
    assert not device_needs_inventory_review("brist-data-sw", phys, {})
    assert not device_needs_inventory_review("cien-data-sw", phys, {})

    topology = {"operational": {"layers": {"physical": {"edges": phys}, "underlay": {"edges": []},
                                           "routing": {"edges": []}}}}
    brist = "\n".join(device_detail_lines(topology)["brist-data-sw"])
    assert "Mapping Unknown: 0" in brist and "Not read:    2" in brist and "not on box" not in brist


def test_device_with_no_interface_records_is_not_called_healthy():
    """scm-data-sw: NSO holds no interfaces for it and it did not answer, so nothing was checked."""
    from diagnostic_mas.operator_report import _health_prose

    phys = [{"id": "if:sw1:a", "local": {"device": "sw1", "interface": "a"}, "state": {"admin": "up", "oper": "up"}}]
    assert device_interfaces_label("scm-data-sw", phys, {}) == "—"
    assert device_interfaces_label("sw1", phys, {}) == "Healthy"
    # An unconfirmed mapping issue for the device still asks for review.
    mismatch = {"if:scm-data-sw:x": {"device": "scm-data-sw", "confirmed": False}}
    assert device_interfaces_label("scm-data-sw", phys, mismatch) == "Inventory Review"
    assert (_health_prose({"interfaces": "—", "hardware": "Unavailable", "notes": ""})
            == "Interface and hardware status not collected for this run")
