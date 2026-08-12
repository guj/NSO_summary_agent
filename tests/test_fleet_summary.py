from pathlib import Path

from agent.fleet_summary_thresholds import (
    FleetSummaryThresholds,
    load_fleet_summary_thresholds,
)
from agent.report_executive import (
    fleet_infra_alert_counts,
    fleet_service_instance_counts,
    format_executive_section,
    format_fleet_summary,
)


def test_thresholds_defaults_when_missing(tmp_path: Path):
    assert load_fleet_summary_thresholds(tmp_path / "nope.yaml") == (
        FleetSummaryThresholds(cpu_five_min_pct=80, memory_used_pct=85)
    )


def test_thresholds_from_yaml(tmp_path: Path):
    path = tmp_path / "t.yaml"
    path.write_text("cpu_five_min_pct: 50\nmemory_used_pct: 70\n", encoding="utf-8")
    thr = load_fleet_summary_thresholds(path)
    assert thr.cpu_five_min_pct == 50
    assert thr.memory_used_pct == 70


def test_service_instance_counts():
    operational, degraded = fleet_service_instance_counts(
        {
            "l2ptp": {"total": 2, "up": 1, "down": 1, "degraded": 0, "unknown": 0},
            "l3rt": {"total": 1, "up": 1, "down": 0, "degraded": 0, "unknown": 0},
        }
    )
    assert operational == 2
    assert degraded == 1


def test_infra_alerts_use_thresholds():
    health = {
        "sw1": {
            "cpu": {"five_min": 85},
            "memory": {"used_pct": 90.0},
        },
        "sw2": {
            "cpu": {"five_min": 10},
            "memory": {"used_pct": 20.0},
        },
        "sw3": {"error": "failed"},
    }
    cpu, mem = fleet_infra_alert_counts(
        health, FleetSummaryThresholds(cpu_five_min_pct=80, memory_used_pct=85)
    )
    assert cpu == 1
    assert mem == 1


def test_format_fleet_summary_shape():
    fleet_sync = {
        "status": "success",
        "data": {
            "summary": {"in_sync": 3, "out_of_sync": 0, "error": 0},
            "devices": [
                {"device": "a", "result": "in-sync"},
                {"device": "b", "result": "in-sync"},
                {"device": "c", "result": "in-sync"},
            ],
        },
    }
    text = format_fleet_summary(
        counts={"l2ptp": {"total": 2, "up": 2, "down": 0, "degraded": 0, "unknown": 0}},
        topology=None,
        fleet_sync=fleet_sync,
        system_health={},
    )
    assert text.startswith("Fleet Summary\n-------------")
    assert "Total: 3" in text
    assert "In Sync: 3" in text
    assert "Out of Sync: 0" in text
    assert "Operational: 2" in text
    assert "Degraded: 0" in text
    assert "CPU Alerts: 0" in text
    assert "Memory Alerts: 0" in text
    assert "Temperature Alerts: 0" in text
    assert "Fan Alerts: 0" in text
    assert "Power Supply Alerts: 0" in text
    assert "Control Plane Drop Alerts: 0" in text
    assert "Devices requiring review: 0" in text


def test_format_fleet_summary_hardware_alerts():
    text = format_fleet_summary(
        counts={},
        topology=None,
        fleet_sync={
            "status": "success",
            "data": {
                "summary": {"in_sync": 1, "out_of_sync": 0, "error": 0},
                "devices": [{"device": "a", "result": "in-sync"}],
            },
        },
        system_health={},
        hardware_health={
            "a": {
                "temperature": [{"ok": False, "status": "Minor"}],
                "fans": [{"ok": True}],
                "power": [{"ok": True}],
                "control_plane": [{"dropped": 2}],
            }
        },
    )
    assert "Temperature Alerts: 1" in text
    assert "Fan Alerts: 0" in text
    assert "Power Supply Alerts: 0" in text
    assert "Control Plane Drop Alerts: 1" in text


def test_executive_inserts_fleet_summary_between_status_and_actions():
    body = format_executive_section(
        run_id="2026-07-15T16:57:00Z",
        counts={},
        topology=None,
        delta={"first_run": True, "counts": {}},
        llm_narrative=(
            "Overall Status\n--------------\n🟢 Services: ok\n\n"
            "Action Items\n------------\n1. Do a thing"
        ),
        rich_markers=True,
        fleet_sync={
            "status": "success",
            "data": {
                "summary": {"in_sync": 1, "out_of_sync": 0, "error": 0},
                "devices": [{"device": "sw1", "result": "in-sync"}],
            },
        },
        system_health={},
    )
    assert body.index("Overall Status") < body.index("Fleet Summary")
    assert body.index("Fleet Summary") < body.index("Action Items")
    assert "1. Do a thing" in body
    assert "In Sync: 1" in body
    assert "Operational Assessment" in body
    assert body.index("Device Health") < body.index("Operational Assessment")


def test_operational_assessment_body_appended():
    body = format_executive_section(
        run_id="2026-07-15T16:57:00Z",
        counts={},
        topology=None,
        delta={"first_run": True, "counts": {}},
        llm_narrative="Overall Status\n--------------\n🟢 ok\n\nAction Items\n------------\nNone reported.",
        rich_markers=True,
        operational_assessment="Routing and services look healthy overall.",
    )
    assert "Operational Assessment\n----------------------" in body
    assert "Routing and services look healthy overall." in body
