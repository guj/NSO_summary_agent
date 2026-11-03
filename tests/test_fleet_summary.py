from pathlib import Path

from nso_facts.fleet_summary_thresholds import (
    FleetSummaryThresholds,
    load_fleet_summary_thresholds,
)
from nso_facts.fleet_rollups import (
    fleet_infra_alert_counts,
    fleet_service_instance_counts,
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
