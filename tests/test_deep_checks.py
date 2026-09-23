"""Tests for deep-check MCP arg normalization."""

from __future__ import annotations

import pytest

from multi_agent.deep_checks import (
    _normalize_exec_show_args,
    _with_device_name,
    run_deep_checks,
)


def test_with_device_name_promotes_device_key():
    assert _with_device_name({"device": "lbnl-data-sw"}) == {
        "device_name": "lbnl-data-sw"
    }
    assert _with_device_name({"device_name": "a", "device": "b"}) == {
        "device_name": "a"
    }


def test_device_name_only_drops_interface_extra():
    from multi_agent.deep_checks import _device_name_only

    assert _device_name_only(
        {"device": "lbnl-data-sw", "interface": "TF0/0/0/23/1"}
    ) == {"device_name": "lbnl-data-sw"}



def test_normalize_exec_show_strips_show_prefix():
    assert _normalize_exec_show_args(
        {"device": "a", "command": "show isis neighbors"}
    ) == {"device_name": "a", "input_command": "isis neighbors"}
    assert _normalize_exec_show_args(
        {"device_name": "a", "input_command": "isis neighbors"}
    ) == {"device_name": "a", "input_command": "isis neighbors"}


@pytest.mark.asyncio
async def test_get_interface_health_drops_interface_kwarg(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_call_mcp(client, tool, params=None):
        calls.append((tool, dict(params or {})))
        return {"ok": True}

    monkeypatch.setattr("multi_agent.deep_checks.call_mcp", fake_call_mcp)
    await run_deep_checks(
        client=None,
        plan=[
            {
                "check": "get_interface_health",
                "args": {
                    "device": "lbnl-data-sw",
                    "interface": "TF0/0/0/23/1",
                },
            }
        ],
    )
    assert calls == [("get_interface_health", {"device_name": "lbnl-data-sw"})]


@pytest.mark.asyncio
async def test_verify_bgp_peer_reachability_sends_device_name(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_call_mcp(client, tool, params=None):
        calls.append((tool, dict(params or {})))
        return {"ok": True}

    monkeypatch.setattr("multi_agent.deep_checks.call_mcp", fake_call_mcp)
    await run_deep_checks(
        client=None,
        plan=[
            {
                "check": "verify_bgp_peer_reachability",
                "args": {"device": "lbnl-data-sw"},
                "reason": "check BGP peer reachability",
            }
        ],
    )
    assert calls == [
        ("verify_bgp_peer_reachability", {"device_name": "lbnl-data-sw"})
    ]


@pytest.mark.asyncio
async def test_exec_show_strips_leading_show(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_call_mcp(client, tool, params=None):
        calls.append((tool, dict(params or {})))
        return {
            "device": "uky-data-sw",
            "command": "show isis neighbors",
            "result": "System Id star-data-sw Hu0/0/0/23.856 Up",
        }

    monkeypatch.setattr("multi_agent.deep_checks.call_mcp", fake_call_mcp)
    evidence = await run_deep_checks(
        client=None,
        plan=[
            {
                "check": "exec_show",
                "args": {
                    "device": "uky-data-sw",
                    "command": "show isis neighbors",
                },
                "reason": "verify adjacency to star-data-sw",
            }
        ],
    )
    assert calls == [
        (
            "exec_show",
            {
                "device_name": "uky-data-sw",
                "input_command": "isis neighbors",
            },
        )
    ]
    assert "error" not in evidence[0]
    assert "result" in evidence[0]


@pytest.mark.asyncio
async def test_exec_show_validation_message_recorded_as_error(monkeypatch):
    async def fake_call_mcp(client, tool, params=None):
        return (
            "input_command must not start with 'show ' — "
            "exec_show prepends it. Got: 'show isis neighbors'"
        )

    monkeypatch.setattr("multi_agent.deep_checks.call_mcp", fake_call_mcp)
    evidence = await run_deep_checks(
        client=None,
        plan=[
            {
                "check": "exec_show",
                "args": {"device": "uky-data-sw", "command": "isis neighbors"},
            }
        ],
    )
    assert "error" in evidence[0]
    assert "must not start with" in evidence[0]["error"]


def test_any_probe_succeeded_ignores_error_only_lists():
    from diagnostic_mas.deep_checks import any_probe_succeeded

    assert any_probe_succeeded(None) is False
    assert any_probe_succeeded([]) is False
    assert any_probe_succeeded([{"error": "timeout"}]) is False
    assert any_probe_succeeded(
        [{"error": "a"}, {"error": "b", "check": "exec_show"}]
    ) is False
    assert any_probe_succeeded(
        [{"error": "a"}, {"check": "exec_show", "result": "UP"}]
    ) is True
    assert any_probe_succeeded([{"check": "exec_show", "ok": True}]) is True
