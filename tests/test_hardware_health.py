import pytest

from agent.hardware_health import (
    collect_hardware_health,
    device_hardware_label,
    fleet_hardware_alert_counts,
    format_device_hardware_section,
)


def test_device_hardware_label():
    assert device_hardware_label(None) == "Unavailable"
    assert device_hardware_label({"error": "x"}) == "Unavailable"
    assert device_hardware_label({}) == "Unavailable"
    assert (
        device_hardware_label(
            {
                "fans": [{"ok": True}],
                "power": [{"ok": True}],
                "control_plane": [{"dropped": 0}],
            }
        )
        == "Healthy"
    )
    assert (
        device_hardware_label(
            {
                "fans": [{"ok": True}],
                "power": [{"ok": True}],
                "control_plane": [{"dropped": 3}],
            }
        )
        == "Review"
    )
    assert (
        device_hardware_label(
            {"temperature": [{"ok": False, "status": "Critical"}]}
        )
        == "Review"
    )


def test_fleet_hardware_alert_counts():
    hw = {
        "a": {
            "temperature": [
                {
                    "sensor": "Inlet",
                    "status": "Critical",
                    "ok": False,
                    "value_celsius": 87,
                }
            ],
            "fans": [{"name": "FT0", "ok": True}],
            "power": [{"location": "0/PM0", "ok": False, "status": "Failed"}],
            "control_plane": [{"flow": "BGP", "accepted": 1, "dropped": 5}],
        },
        "b": {
            "temperature": [{"sensor": "x", "status": "Normal", "ok": True}],
            "fans": [],
            "power": [{"location": "0/PM0", "ok": True}],
            "control_plane": [{"flow": "ISIS", "accepted": 1, "dropped": 0}],
        },
        "c": {"error": "timeout"},
    }
    temp, fan, power, cp = fleet_hardware_alert_counts(hw)
    assert temp == 1
    assert fan == 0
    assert power == 1
    assert cp == 1


def test_format_hardware_healthy():
    entry = {
        "temperature": [
            {"sensor": "Inlet", "status": "Normal", "ok": True, "value_celsius": 30}
        ],
        "fans": [
            {"name": "FT0", "ok": True},
            {"name": "FT1", "ok": True},
        ],
        "power": [
            {"location": "0/PM0", "ok": True},
            {"location": "0/PM1", "ok": True},
        ],
        "control_plane": [{"flow": "BGP", "dropped": 0}],
    }
    text = "\n".join(format_device_hardware_section(entry))
    assert "Hardware" in text
    assert "Normal Sensors: 1, Warning: 0, Critical: 0" in text
    assert "Total: 2, Healthy: 2, Failed: 0" in text
    assert "Installed: 2, Healthy: 2, Failed: 0" in text
    assert "Drops: 0" in text


def test_format_hardware_issues_and_unavailable():
    entry = {
        "temperature": [
            {
                "sensor": "Inlet Sensor",
                "status": "Critical",
                "ok": False,
                "value_celsius": 87,
            }
        ],
        "fans": [{"name": "Fan Tray 2", "ok": False}],
        "power": [],
        "control_plane": [
            {"flow": "BGP", "dropped": 10000},
            {"flow": "ISIS", "dropped": 5231},
        ],
    }
    text = "\n".join(format_device_hardware_section(entry))
    assert "Critical:" in text
    assert "Inlet Sensor: 87°C" in text
    assert "Failed:" in text
    assert "Fan Tray 2" in text
    assert "  Power Supplies\n    Unavailable" in text
    assert "15,231 packets" in text


def test_format_hardware_error_entry():
    text = "\n".join(format_device_hardware_section({"error": "nso_error"}))
    assert text.count("Unavailable") == 4


class _FakeClient:
    pass


@pytest.mark.asyncio
async def test_collect_hardware_health_strips_raw_and_errors(monkeypatch):
    async def fake_call(client, tool, params=None):
        assert tool == "get_hardware_health"
        name = (params or {}).get("device_name")
        if name == "bad":
            return {"status": "error", "error_message": "boom"}
        return {
            "status": "success",
            "data": {
                "device": name,
                "outcome": "ok",
                "summary": {"total": 1, "ok": 1, "failed": 0},
                "temperature": [{"sensor": "a", "ok": True, "status": "Normal"}],
                "fans": [{"name": "f", "ok": True}],
                "power": [{"location": "0/PM0", "ok": True}],
                "control_plane": [{"flow": "BGP", "dropped": 0}],
                "raw": {"temperature": "HUGE"},
            },
        }

    monkeypatch.setattr("nso_facts.hardware_health.call_mcp", fake_call)
    out = await collect_hardware_health(_FakeClient(), ["good", "bad"])
    assert "raw" not in out["good"]
    assert out["good"]["temperature"][0]["sensor"] == "a"
    assert out["bad"] == {"error": "boom"}
