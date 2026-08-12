import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent.config import Settings
from agent.iface_troubleshoot import (
    collect_interface_troubleshooting,
    error_counts_from_interface_health,
    execute_troubleshoot_tool,
    is_tool_allowed,
    select_troubleshoot_target,
    troubleshoot_interface,
)


def _phys(device: str, iface: str, admin: str, oper: str) -> dict:
    return {
        "id": f"if:{device}:{iface}",
        "local": {"device": device, "interface": iface},
        "state": {"admin": admin, "oper": oper, "status": "up" if oper == "up" else "down"},
    }


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
        "report_sections": (
            "problems",
            "counts",
            "delta",
            "fleet_sync",
            "devices",
            "ignored_types",
        ),
        "slack_webhook_url": None,
        "smtp_host": None,
        "smtp_port": 587,
        "smtp_user": None,
        "smtp_password": None,
        "smtp_use_tls": True,
        "email_from": None,
        "email_to": [],
        "email_subject_prefix": "NSO Summary",
        "prometheus_pushgateway_url": None,
        "prometheus_job": "nso-summary",
        "prometheus_instance": "default",
        "prompts_dir": Path(__file__).resolve().parents[1] / "prompts",
        "topology_force_update": False,
        "interface_equivalences_file": None,
        "iface_troubleshoot_max_tool_rounds": 10,
        "iface_troubleshoot_disable": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_select_first_up_down_by_sorted_name():
    edges = [
        _phys("sw1", "Hu0/0/0/5", "up", "down"),
        _phys("sw1", "Hu0/0/0/3", "up", "down"),
        _phys("sw1", "Hu0/0/0/1", "up", "up"),
    ]
    assert select_troubleshoot_target("sw1", edges, {}) == ("Hu0/0/0/3", "up_down")


def test_select_worst_errors_when_no_up_down():
    edges = [_phys("sw1", "Hu0/0/0/1", "up", "up")]
    errors = {"Hu0/0/0/9": 10, "Hu0/0/0/2": 50}
    assert select_troubleshoot_target("sw1", edges, errors) == ("Hu0/0/0/2", "errors")


def test_select_none_when_healthy():
    assert select_troubleshoot_target("sw1", [_phys("sw1", "Lo0", "up", "up")], {}) is None


def test_error_counts_from_health_blob():
    payload = {
        "status": "success",
        "data": {
            "interfaces": [
                {"name": "Hu0/0/0/1", "input_errors": 0, "crc": 0},
                {"name": "Hu0/0/0/2", "input_errors": 12, "crc": 3},
            ],
            "flagged": [{"name": "Hu0/0/0/9", "errors": 5}],
        },
    }
    counts = error_counts_from_interface_health(payload)
    assert counts["Hu0/0/0/2"] == 15
    assert counts["Hu0/0/0/9"] == 5
    assert "Hu0/0/0/1" not in counts


def test_deny_sync_and_active_probes():
    assert not is_tool_allowed("sync_from_device")
    assert not is_tool_allowed("exec_ping")
    assert not is_tool_allowed("exec_traceroute")
    assert not is_tool_allowed("verify_bgp_peer_reachability")
    assert is_tool_allowed("exec_show")
    assert is_tool_allowed("get_interface_health")


@pytest.mark.asyncio
async def test_execute_forces_device_and_blocks_deny(monkeypatch):
    calls = []

    async def fake_call_mcp(client, tool, params=None):
        calls.append((tool, params))
        return {"ok": True}

    monkeypatch.setattr("agent.iface_troubleshoot.call_mcp", fake_call_mcp)
    out = await execute_troubleshoot_tool(
        None,
        "sw1",
        "exec_show",
        {"device_name": "other", "input_command": "interfaces Hu0/0/0/1"},
    )
    assert "ok" in out
    assert calls[0][1]["device_name"] == "sw1"
    denied = await execute_troubleshoot_tool(
        None, "sw1", "exec_ping", {"target": "1.1.1.1"}
    )
    assert "denied" in denied.lower()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_troubleshoot_interface_tool_loop(monkeypatch):
    class _Fn:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class _Call:
        def __init__(self, id_, name, arguments):
            self.id = id_
            self.function = _Fn(name, arguments)

    class _Msg:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, message):
            self.message = message

    class _Resp:
        def __init__(self, message):
            self.choices = [_Choice(message)]

    rounds = {"n": 0}

    class _Completions:
        def create(self, **kwargs):
            rounds["n"] += 1
            if rounds["n"] == 1:
                return _Resp(
                    _Msg(
                        tool_calls=[
                            _Call(
                                "c1",
                                "mcp_call",
                                json.dumps(
                                    {
                                        "tool_name": "exec_show",
                                        "params": {
                                            "input_command": "interfaces Hu0/0/0/3"
                                        },
                                    }
                                ),
                            )
                        ]
                    )
                )
            return _Resp(
                _Msg(
                    content=json.dumps(
                        {
                            "summary": "Protocol down; CRC climbing — suspect L1",
                            "detail": "input errors rising",
                        }
                    )
                )
            )

    class _Client:
        chat = type("C", (), {"completions": _Completions()})()

    async def fake_exec(client, device, name, arguments):
        return json.dumps({"result": "intf down, crc 9"})

    monkeypatch.setattr(
        "agent.summarize.fabric_openai_client", lambda settings: _Client()
    )
    monkeypatch.setattr(
        "agent.iface_troubleshoot.execute_troubleshoot_tool", fake_exec
    )
    entry = await troubleshoot_interface(
        None,
        _settings(),
        device="sw1",
        interface="Hu0/0/0/3",
        reason="up_down",
    )
    assert entry["summary"].startswith("Protocol down")
    assert entry["interface"] == "Hu0/0/0/3"
    assert entry["tools_used"]


@pytest.mark.asyncio
async def test_collect_writes_operational_blob(monkeypatch):
    topology = {
        "static": {
            "layers": {
                "physical": {
                    "edges": [
                        {
                            "id": "if:sw1:Hu0/0/0/3",
                            "local": {"device": "sw1", "interface": "Hu0/0/0/3"},
                        }
                    ]
                }
            }
        },
        "operational": {
            "layers": {
                "physical": {
                    "edges": [
                        {
                            "id": "if:sw1:Hu0/0/0/3",
                            "state": {
                                "admin": "up",
                                "oper": "down",
                                "status": "down",
                            },
                        }
                    ]
                }
            }
        },
    }

    async def fake_call_mcp(client, tool, params=None):
        return {"interfaces": []}

    async def fake_ts(client, settings, *, device, interface, reason, context=None):
        return {
            "interface": interface,
            "reason": reason,
            "summary": "CRC climbing",
            "tools_used": ["exec_show"],
        }

    monkeypatch.setattr("agent.iface_troubleshoot.call_mcp", fake_call_mcp)
    monkeypatch.setattr(
        "agent.iface_troubleshoot.troubleshoot_interface", fake_ts
    )
    out = await collect_interface_troubleshooting(None, _settings(), topology)
    assert out["sw1"]["summary"] == "CRC climbing"
    assert (
        topology["operational"]["interface_troubleshooting"]["sw1"]["reason"]
        == "up_down"
    )


@pytest.mark.asyncio
async def test_collect_skips_when_disabled():
    topology = {"operational": {"layers": {"physical": {"edges": []}}}}
    out = await collect_interface_troubleshooting(
        None, _settings(iface_troubleshoot_disable=True), topology
    )
    assert out == {}
    assert "interface_troubleshooting" not in topology["operational"]


@pytest.mark.asyncio
async def test_e2e_mocked_health_and_loop(monkeypatch):
    topology = {
        "static": {
            "layers": {
                "physical": {
                    "edges": [
                        {
                            "id": "if:sw1:Hu0/0/0/3",
                            "local": {"device": "sw1", "interface": "Hu0/0/0/3"},
                        }
                    ]
                }
            }
        },
        "operational": {
            "layers": {
                "physical": {
                    "edges": [
                        {
                            "id": "if:sw1:Hu0/0/0/3",
                            "state": {
                                "admin": "up",
                                "oper": "down",
                                "status": "down",
                            },
                        }
                    ]
                }
            }
        },
    }

    async def fake_call_mcp(client, tool, params=None):
        if tool == "get_interface_health":
            return {"interfaces": [{"name": "Hu0/0/0/3", "crc": 1}]}
        return {"ok": True}

    class _Fn:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class _Call:
        id = "c1"
        function = _Fn(
            "mcp_call",
            json.dumps(
                {
                    "tool_name": "exec_show",
                    "params": {"input_command": "interfaces Hu0/0/0/3"},
                }
            ),
        )

    class _Msg:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, message):
            self.message = message

    class _Resp:
        def __init__(self, message):
            self.choices = [_Choice(message)]

    n = {"i": 0}

    class _Completions:
        def create(self, **kwargs):
            n["i"] += 1
            if n["i"] == 1:
                return _Resp(_Msg(tool_calls=[_Call()]))
            return _Resp(
                _Msg(content='{"summary": "L1 errors on Hu0/0/0/3", "detail": ""}')
            )

    class _Client:
        chat = type("C", (), {"completions": _Completions()})()

    monkeypatch.setattr("agent.iface_troubleshoot.call_mcp", fake_call_mcp)
    monkeypatch.setattr(
        "agent.summarize.fabric_openai_client", lambda settings: _Client()
    )
    out = await collect_interface_troubleshooting(None, _settings(), topology)
    assert out["sw1"]["reason"] == "up_down"
    assert "L1 errors" in out["sw1"]["summary"]
