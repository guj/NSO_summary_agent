"""Tests for Phase 1+ Pushgateway metrics."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agent.config import Settings
from agent.metrics import (
    build_phase1_metrics,
    format_exposition,
    push_phase1_metrics,
    pushgateway_url,
)


def _settings(**overrides) -> Settings:
    defaults = {
        "mcp_server_cmd": "x",
        "mcp_server_args": [],
        "mcp_env": {},
        "fabric_api_key": "k",
        "fabric_api_url": "https://example.com",
        "fabric_model": "m",
        "state_dir": Path("/tmp"),
        "dry_run": False,
        "ignore_service_types": frozenset(),
        "max_service_types": 10,
        "report_sections": ("executive", "devices"),
        "slack_webhook_url": None,
        "smtp_host": None,
        "smtp_port": 587,
        "smtp_user": None,
        "smtp_password": None,
        "smtp_use_tls": True,
        "email_from": None,
        "email_to": [],
        "email_subject_prefix": "NSO Summary",
        "prometheus_pushgateway_url": "http://127.0.0.1:9091",
        "prometheus_job": "nso-summary",
        "prometheus_instance": "laptop",
        "prompts_dir": Path("/tmp"),
        "topology_force_update": False,
        "interface_equivalences_file": None,
        "iface_troubleshoot_max_tool_rounds": 10,
        "iface_troubleshoot_disable": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_pushgateway_url_none_when_unset():
    assert pushgateway_url(_settings(prometheus_pushgateway_url=None)) is None
    assert pushgateway_url(_settings(prometheus_pushgateway_url="")) is None


def test_pushgateway_url_encodes_job_instance():
    url = pushgateway_url(
        _settings(
            prometheus_pushgateway_url="http://127.0.0.1:9091/",
            prometheus_job="nso-summary",
            prometheus_instance="dec laptop",
        )
    )
    assert url == (
        "http://127.0.0.1:9091/metrics/job/nso-summary/instance/dec%20laptop"
    )


def test_build_phase1_metrics_from_snapshot():
    snapshot = {
        "fleet_sync": {
            "status": "success",
            "data": {
                "summary": {"in_sync": 2, "out_of_sync": 1, "error": 1},
                "devices": [
                    {"device": "a"},
                    {"device": "b"},
                    {"device": "c"},
                    {"device": "d"},
                ],
            },
        },
        "counts": {
            "l2ptp": {"total": 2, "up": 1, "down": 1, "degraded": 0, "unknown": 0},
            "l3vpn": {"total": 0, "up": 0, "down": 0, "degraded": 0, "unknown": 0},
        },
        "system_health": {
            "a": {"cpu": {"five_min": 90}, "memory": {"used_pct": 50}},
            "b": {"cpu": {"five_min": 10}, "memory": {"used_pct": 95}},
        },
        "hardware_health": {
            "a": {
                "temperature": [{"ok": False}],
                "fans": [{"ok": True}],
                "power": [{"ok": True}],
                "control_plane": [{"dropped": 0}],
            },
            "b": {
                "temperature": [{"ok": True}],
                "fans": [{"ok": False}],
                "power": [{"ok": True}],
                "control_plane": [{"dropped": 3}],
            },
        },
        "topology": {
            "operational": {
                "layers": {
                    "physical": {"summary": {"total": 4, "up": 3, "down": 1}},
                    "underlay": {"summary": {"total": 3, "up": 2, "down": 1}},
                    "routing": {"summary": {"total": 3, "up": 3, "down": 0}},
                },
                "issues": [
                    {"layer": "physical", "message": "x"},
                    {"layer": "physical", "message": "y"},
                    {"layer": "underlay", "message": "z"},
                ],
            }
        },
    }
    delta = {
        "first_run": False,
        "new_failures": ["l2ptp/a"],
        "recoveries": ["l2ptp/b", "l2ptp/c"],
        "status_changes": [{"name": "x"}],
        "removed": [],
    }
    text = format_exposition(
        build_phase1_metrics(
            snapshot,
            success=True,
            duration_seconds=12.5,
            timestamp_seconds=1_700_000_000,
            pipeline="agent",
            delta=delta,
        )
    )
    assert 'nso_summary_run_success{pipeline="agent"} 1.0' in text
    assert 'nso_summary_last_run_timestamp_seconds{pipeline="agent"} 1700000000.0' in text
    assert 'nso_summary_run_duration_seconds{pipeline="agent"} 12.5' in text
    assert 'nso_fleet_devices_total{pipeline="agent"} 4.0' in text
    assert 'nso_fleet_devices_in_sync{pipeline="agent"} 2.0' in text
    assert 'nso_fleet_devices_out_of_sync{pipeline="agent"} 1.0' in text
    assert 'nso_fleet_devices_sync_error{pipeline="agent"} 1.0' in text
    assert 'nso_services_up{pipeline="agent",service_type="l2ptp"} 1.0' in text
    assert 'nso_services_down{pipeline="agent",service_type="l2ptp"} 1.0' in text
    assert 'nso_isis_adjacencies_up{pipeline="agent"} 2.0' in text
    assert 'nso_isis_adjacencies_down{pipeline="agent"} 1.0' in text
    assert 'nso_bgp_sessions_up{pipeline="agent"} 3.0' in text
    assert 'nso_bgp_sessions_down{pipeline="agent"} 0.0' in text
    assert 'nso_physical_links_up{pipeline="agent"} 3.0' in text
    assert 'nso_physical_links_down{pipeline="agent"} 1.0' in text
    assert 'nso_topology_issues{layer="physical",pipeline="agent"} 2.0' in text
    assert 'nso_topology_issues{layer="underlay",pipeline="agent"} 1.0' in text
    assert 'nso_infra_cpu_alerts{pipeline="agent"} 1.0' in text
    assert 'nso_infra_memory_alerts{pipeline="agent"} 1.0' in text
    assert 'nso_hardware_temperature_alerts{pipeline="agent"} 1.0' in text
    assert 'nso_hardware_fan_alerts{pipeline="agent"} 1.0' in text
    assert 'nso_hardware_power_alerts{pipeline="agent"} 0.0' in text
    assert 'nso_hardware_control_plane_drop_alerts{pipeline="agent"} 1.0' in text
    assert 'nso_inventory_review_devices{pipeline="agent"}' in text
    assert 'nso_delta_first_run{pipeline="agent"} 0.0' in text
    assert 'nso_delta_new_failures{pipeline="agent"} 1.0' in text
    assert 'nso_delta_recoveries{pipeline="agent"} 2.0' in text
    assert 'nso_delta_status_changes{pipeline="agent"} 1.0' in text
    assert 'nso_delta_removed{pipeline="agent"} 0.0' in text


def test_pipeline_multi_agent_label():
    text = format_exposition(
        build_phase1_metrics(
            {"counts": {}, "topology": {}},
            success=True,
            duration_seconds=1.0,
            timestamp_seconds=1.0,
            pipeline="multi-agent",
        )
    )
    assert 'pipeline="multi-agent"' in text


def test_push_skips_when_url_unset():
    assert (
        push_phase1_metrics(
            {},
            _settings(prometheus_pushgateway_url=None),
            success=True,
            duration_seconds=1.0,
        )
        is False
    )


def test_push_success(monkeypatch):
    calls: list[object] = []

    class _Resp:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=15):
        calls.append(req)
        return _Resp()

    monkeypatch.setattr("nso_facts.metrics.urllib.request.urlopen", fake_urlopen)
    ok = push_phase1_metrics(
        {
            "fleet_sync": {"status": "error"},
            "counts": {},
            "topology": {},
        },
        _settings(),
        success=True,
        duration_seconds=1.0,
        timestamp_seconds=1.0,
        pipeline="agent",
    )
    assert ok is True
    assert len(calls) == 1
    req = calls[0]
    assert req.full_url.endswith("/metrics/job/nso-summary/instance/laptop")
    assert req.data.decode("utf-8").startswith("# TYPE nso_summary_run_success")


def test_push_failure_warns(capsys, monkeypatch):
    monkeypatch.setattr(
        "nso_facts.metrics.urllib.request.urlopen",
        MagicMock(side_effect=OSError("connection refused")),
    )
    ok = push_phase1_metrics(
        {},
        _settings(),
        success=False,
        duration_seconds=0.1,
    )
    assert ok is False
    err = capsys.readouterr().err
    assert "Pushgateway" in err


def test_push_http_error_appends_response_body(capsys, monkeypatch):
    import io
    import urllib.error

    exc = urllib.error.HTTPError(
        url="http://127.0.0.1:9091/metrics/job/nso-summary/instance/laptop",
        code=400,
        msg="Bad Request",
        hdrs={},
        fp=io.BytesIO(b"text format parsing error in line 3"),
    )
    monkeypatch.setattr(
        "nso_facts.metrics.urllib.request.urlopen",
        MagicMock(side_effect=exc),
    )
    ok = push_phase1_metrics(
        {},
        _settings(),
        success=False,
        duration_seconds=0.1,
    )
    assert ok is False
    err = capsys.readouterr().err
    assert "HTTP Error 400" in err
    assert "text format parsing error in line 3" in err
