from agent.report_executive import (
    abbreviate_route_total,
    build_action_context,
    build_device_health_rows,
    device_interfaces_label,
    format_executive_section,
    format_service_summary_lines,
    mismatch_phrasing_examples,
    replace_markers_for_plain,
)


def test_abbreviate_routes():
    assert abbreviate_route_total(446595).endswith("k")
    assert abbreviate_route_total(500) == "500"
    assert abbreviate_route_total(None) == "—"


def test_service_summary_lines():
    lines = format_service_summary_lines(
        {"l2bridge": {"total": 2, "up": 2}, "l3rt": {"total": 1, "up": 1}}
    )
    assert any("l2bridge" in line and "2/2 Up" in line for line in lines)
    assert any("l3rt" in line and "1/1 Up" in line for line in lines)


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


def test_build_health_rows_and_executive_assemble():
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
            "route_summary": {"sw1": {"total": 1000, "sources": {}}},
        },
    }
    rows = build_device_health_rows(topology)
    assert rows and rows[0]["device"] == "sw1"
    assert rows[0]["interfaces"] == "Healthy"
    assert rows[0]["bgp"] == "1/1"
    assert rows[0]["sync"] == "—"

    body = format_executive_section(
        run_id="2026-07-15T16:57:00Z",
        counts={"l3rt": {"total": 1, "up": 1}},
        topology=topology,
        delta={"first_run": True, "counts": {}},
        llm_narrative="Overall Status\n--------------\n🟢 Services: ok\n\nAction Items\n------------\nNone reported.",
        rich_markers=True,
        fleet_sync={
            "status": "success",
            "data": {
                "summary": {"in_sync": 1, "out_of_sync": 0, "error": 0},
                "devices": [{"device": "sw1", "result": "in-sync"}],
            },
        },
        fabric_api_url="https://ai.fabric-testbed.net",
        fabric_model="gpt-oss-20b",
        mcp_server_cmd="/opt/cisco-nso-mcp-server",
    )
    assert "NSO Operations Snapshot" in body
    assert "2026-07-15 16:57 UTC" in body
    assert "LLM: FABRIC AI (ai.fabric-testbed.net)  model=gpt-oss-20b" in body
    assert "MCP: /opt/cisco-nso-mcp-server" in body
    assert "🟢 Services" in body
    assert "Service Summary" in body
    assert "l3rt          1/1 Up" in body
    assert "Device Health" in body
    assert "sw1" in body
    assert "Sync" in body
    assert "in-sync" in body

    plain = format_executive_section(
        run_id="2026-07-15T16:57:00Z",
        counts={},
        topology=topology,
        delta={"first_run": False, "counts": {}},
        llm_narrative="Overall Status\n--------------\n🟢 Services: ok\n",
        rich_markers=False,
    )
    assert "OK Services" in plain
    assert "🟢" not in plain


def test_device_health_notes_skip_admin_down_and_down_down():
    from agent.report_executive import device_notes_label

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
    assert device_notes_label("uky-data-sw", phys, {}) == (
        "Mapping unknown — see Detailed Analysis"
    )


def test_device_health_sync_column():
    from agent.report_executive import build_device_health_rows, format_device_health_table

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
            "route_summary": {"sw1": {"total": 10, "sources": {}}},
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
    table = format_device_health_table(rows)
    assert "Sync" in table
    assert "Hardware" in table
    assert "out-of-sync" in table


def test_device_health_hardware_column():
    from agent.report_executive import build_device_health_rows, format_device_health_table

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
            "route_summary": {"sw1": {"total": 10, "sources": {}}},
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
    table = format_device_health_table(rows)
    assert "Hardware" in table
    assert "Review" in table


def test_replace_markers():
    assert replace_markers_for_plain("🟢 x 🟡 y 🔴 z") == "OK x ATTN y WARN z"


def test_mismatch_phrasing_examples():
    items = mismatch_phrasing_examples(
        [
            {
                "device": "renc-data-sw",
                "kind": "type_change",
                "confirmed": False,
            },
            {
                "device": "renc-data-sw",
                "kind": "breakout",
                "confirmed": False,
            },
            {
                "device": "renc-data-sw",
                "kind": "missing",
                "confirmed": False,
            },
            {
                "device": "uky-data-sw",
                "kind": "missing",
                "confirmed": False,
            },
        ]
    )
    assert items == [
        "Review and confirm the 4 NSO↔device interface mappings on "
        "renc-data-sw and uky-data-sw",
        "Resolve interface type/breakout mismatches on renc-data-sw",
        "Confirm missing-on-box interface(s) on renc-data-sw and uky-data-sw",
    ]


def test_action_context_covers_services_sync_and_inventory():
    ctx = build_action_context(
        counts={"l2ptp": {"total": 2, "up": 1, "down": 1, "degraded": 0, "unknown": 0}},
        fleet_sync={
            "status": "success",
            "data": {
                "devices": [
                    {"device": "renc-data-sw", "result": "out-of-sync"},
                    {"device": "uky-data-sw", "result": "in-sync"},
                ]
            },
        },
        mismatches=[
            {
                "device": "renc-data-sw",
                "kind": "type_change",
                "confirmed": False,
            }
        ],
        delta={"new_failures": ["svc-a"]},
    )
    assert ctx["services_needing_attention"][0]["type"] == "l2ptp"
    assert ctx["devices_not_in_sync"] == [
        {"device": "renc-data-sw", "result": "out-of-sync"}
    ]
    assert ctx["inventory"]["unconfirmed_mapping_count"] == 1
    assert ctx["new_service_failures"] == ["svc-a"]
    assert ctx["inventory"]["phrasing_examples"]


def test_format_executive_keeps_llm_action_items():
    body = format_executive_section(
        run_id="2026-07-15T16:57:00Z",
        counts={},
        topology=None,
        delta={"first_run": True, "counts": {}},
        llm_narrative=(
            "Overall Status\n--------------\n🟢 Services: ok\n\n"
            "Action Items\n------------\n"
            "1. Restore NSO sync on renc-data-sw (out-of-sync)\n"
            "2. Investigate down l2ptp services"
        ),
        rich_markers=True,
    )
    assert "Restore NSO sync on renc-data-sw" in body
    assert "Investigate down l2ptp services" in body
    assert body.index("Overall Status") < body.index("Action Items")
