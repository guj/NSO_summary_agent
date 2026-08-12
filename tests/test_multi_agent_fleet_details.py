"""Tests for inventory/hardware follow-ups in multi-agent reports."""

from __future__ import annotations

from multi_agent.base import AgentResult
from multi_agent.executive import build_action_items
from multi_agent.fleet_details import (
    build_fleet_followup_actions,
    format_inventory_hardware_section,
    hardware_review_reasons,
)
from multi_agent.merge import build_merged_report


def _fleet_pack() -> dict:
    return {
        "physical_issues": [
            {
                "layer": "physical",
                "code": "config_live_mismatch",
                "message": (
                    "renc-data-sw FourHundredGigE0/0/0/32: candidates: Hu0/0/0/32"
                ),
            },
            {
                "layer": "physical",
                "code": "unexpected_live_object",
                "message": (
                    "uky-data-sw BV50000: live interface not present in static config"
                ),
            },
        ],
        "hardware_health": {
            "renc-data-sw": {
                "temperature": [{"ok": True}],
                "fans": [{"ok": True}],
                "power": [{"ok": True}],
                "control_plane": [{"dropped": 12}],
            },
            "lbnl-data-sw": {
                "temperature": [{"ok": True}],
                "fans": [{"ok": True}],
                "power": [{"ok": True}],
                "control_plane": [{"dropped": 0}],
            },
        },
        "topology": {"operational": {"issues": []}},
        "counts": {},
        "delta": {"first_run": True},
    }


def test_hardware_review_reasons_control_plane_drops():
    reasons = hardware_review_reasons(_fleet_pack()["hardware_health"])
    assert "renc-data-sw" in reasons
    assert any("control-plane drops" in r for r in reasons["renc-data-sw"])
    assert "lbnl-data-sw" not in reasons


def test_fleet_followup_actions_include_inventory_and_hw():
    actions = build_fleet_followup_actions(_fleet_pack())
    assert any("renc-data-sw" in a and "mismatch" in a for a in actions)
    assert any("uky-data-sw" in a and "unexpected" in a for a in actions)
    assert any("renc-data-sw" in a and "hardware Review" in a for a in actions)


def test_action_items_routing_only_not_fleet_mirror():
    bgp = AgentResult(
        name="bgp",
        layer="routing",
        issues=[
            {
                "severity": "medium",
                "code": "unknown_neighbor_address",
                "message": "lbnl-data-sw 10.148.0.1: could not map neighbor address",
            }
        ],
    )
    actions = build_action_items([bgp], fleet_pack=_fleet_pack(), limit=10)
    assert any("10.148.0.1" in a for a in actions)
    assert not any("inventory" in a for a in actions)
    assert not any("hardware Review" in a for a in actions)


def test_detailed_section_lists_mismatch():
    text = format_inventory_hardware_section(_fleet_pack())
    assert "## Inventory / hardware" in text
    assert "FourHundredGigE0/0/0/32" in text
    assert "control-plane drops=12" in text


def test_merged_report_includes_inventory_section():
    report = build_merged_report(
        [AgentResult(name="isis", layer="underlay")],
        fleet_pack=_fleet_pack(),
        run_id="20260811T120000Z",
        llm_skipped=True,
    )
    assert "Inventory / hardware" in report
    assert "FourHundredGigE0/0/0/32" in report
