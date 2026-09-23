"""Tests for underlay (IS-IS) topology collectors."""

from __future__ import annotations

from typing import Any

import pytest

from agent.topology.graph import isis_edge_id
from agent.topology.underlay import (
    AdjacencyObservation,
    collect_operational_underlay,
    collect_static_underlay,
    pair_adjacency_observations,
    parse_configured_isis_interfaces,
    resolve_system_id,
)

IOS_XR_ISIS_CONFIG = {
    "status": "success",
    "data": {
        "tailf-ned-cisco-ios-xr:router": {
            "isis": {
                "interfaces": {
                    "interface": [
                        {"interface-name": "HundredGigE0/0/0/0.2400"},
                        {"interface-name": "HundredGigE0/0/0/0.2402"},
                    ]
                }
            }
        }
    },
}

ISIS_ADJ_LBNL = {
    "status": "success",
    "data": {
        "device": "lbnl-data-sw",
        "adjacencies": [
            {
                "system_id": "renc-data-sw",
                "interface": "HundredGigE0/0/0/0.2402",
                "state": "Up",
            }
        ],
        "non_up": [],
        "raw": "",
    },
}

ISIS_ADJ_RENC = {
    "status": "success",
    "data": {
        "device": "renc-data-sw",
        "adjacencies": [
            {
                "system_id": "lbnl-data-sw",
                "interface": "HundredGigE0/0/0/0.2400",
                "state": "Up",
            }
        ],
        "non_up": [],
        "raw": "",
    },
}


def test_isis_edge_id_orders_lexicographically():
    assert isis_edge_id("b", "if2", "a", "if1") == "isis:a:if1:b:if2"


def test_resolve_system_id_matches_device_name():
    devices = ["lbnl-data-sw", "renc-data-sw", "uky-data-sw"]
    assert resolve_system_id("renc-data-sw", devices) == "renc-data-sw"


def test_parse_configured_isis_interfaces_ios_xr():
    names = parse_configured_isis_interfaces(IOS_XR_ISIS_CONFIG)
    assert names == ["HundredGigE0/0/0/0.2400", "HundredGigE0/0/0/0.2402"]


@pytest.mark.asyncio
async def test_static_isis_prefer_get_device_config(monkeypatch):
    from nso_facts.topology import underlay as underlay_mod

    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_call_mcp(client, tool, params=None):
        calls.append((tool, params))
        assert tool == "get_device_config"
        return IOS_XR_ISIS_CONFIG

    monkeypatch.setattr(underlay_mod, "call_mcp", fake_call_mcp)
    names = await underlay_mod._static_isis_interfaces_for_device(object(), "sw1")
    assert names == ["HundredGigE0/0/0/0.2400", "HundredGigE0/0/0/0.2402"]
    assert calls == [("get_device_config", {"device_name": "sw1"})]


@pytest.mark.asyncio
async def test_static_isis_explore_fallback_when_config_empty(monkeypatch):
    from nso_facts.topology import underlay as underlay_mod

    calls: list[str] = []

    async def fake_call_mcp(client, tool, params=None):
        calls.append(tool)
        if tool == "get_device_config":
            return {"status": "success", "data": {"config": {}}}
        if tool == "explore_nso_path":
            path = (params or {}).get("path", "")
            depth = (params or {}).get("depth")
            if path.endswith("/config") and depth == 1:
                return {
                    "status": "success",
                    "data": {"tailf-ned-cisco-ios-xr:router": {}},
                }
            if "router/isis" in path or path.endswith(":router"):
                return IOS_XR_ISIS_CONFIG
            return {"status": "success", "data": {}}
        raise AssertionError(f"unexpected {tool} {params}")

    monkeypatch.setattr(underlay_mod, "call_mcp", fake_call_mcp)
    names = await underlay_mod._static_isis_interfaces_for_device(object(), "sw1")
    assert names == ["HundredGigE0/0/0/0.2400", "HundredGigE0/0/0/0.2402"]
    assert calls[0] == "get_device_config"
    assert "explore_nso_path" in calls


def test_pair_adjacency_observations_bidirectional_up():
    observations = [
        AdjacencyObservation(
            "lbnl-data-sw", "HundredGigE0/0/0/0.2402", "renc-data-sw", "Up"
        ),
        AdjacencyObservation(
            "renc-data-sw", "HundredGigE0/0/0/0.2400", "lbnl-data-sw", "Up"
        ),
    ]
    devices = ["lbnl-data-sw", "renc-data-sw", "uky-data-sw"]
    edges, issues = pair_adjacency_observations(observations, devices)
    assert len(edges) == 1
    assert edges[0]["state"]["status"] == "up"
    assert not any(i["code"] == "unidirectional_adjacency" for i in issues)


def test_pair_adjacency_observations_unidirectional():
    observations = [
        AdjacencyObservation(
            "lbnl-data-sw", "HundredGigE0/0/0/0.2402", "renc-data-sw", "Up"
        ),
        AdjacencyObservation(
            "renc-data-sw", "HundredGigE0/0/0/0.2400", "lbnl-data-sw", "Init"
        ),
    ]
    edges, issues = pair_adjacency_observations(
        observations, ["lbnl-data-sw", "renc-data-sw"]
    )
    assert edges[0]["state"]["status"] == "unidirectional"
    assert any(i["code"] == "unidirectional_adjacency" for i in issues)


class FakeClient:
    def __init__(self, handlers: dict[tuple[str, str], Any]):
        self.handlers = handlers

    async def call_tool(self, tool: str, payload: dict[str, Any]):
        params = payload.get("params") or {}
        key = (tool, _handler_key(tool, params))
        if key not in self.handlers:
            raise AssertionError(f"unexpected tool call: {tool} {params}")
        return _wrap(self.handlers[key])


def _handler_key(tool: str, params: dict[str, Any]) -> str:
    if tool in ("explore_nso_path",):
        return params.get("path", "")
    if tool in ("get_interface_health", "check_isis_adjacencies", "get_device_config"):
        return params.get("device_name", params.get("path", ""))
    return repr(sorted(params.items()))


def _wrap(data: Any):
    class Result:
        def __init__(self, value: Any):
            self.data = value
            self.structured_content = None
            self.content = []

    return Result(data)


@pytest.mark.asyncio
async def test_collect_static_underlay_matches_abbreviated_live_interfaces():
    """Live show isis uses Hu/Fo; NSO config uses HundredGigE/FortyGigE."""
    lbnl_config = {
        "status": "success",
        "data": {
            "tailf-ned-cisco-ios-xr:router": {
                "isis": {
                    "interfaces": {
                        "interface": [
                            {"interface-name": "HundredGigE0/0/0/0.2401"},
                        ]
                    }
                }
            }
        },
    }
    uky_config = {
        "status": "success",
        "data": {
            "tailf-ned-cisco-ios-xr:router": {
                "isis": {
                    "interfaces": {
                        "interface": [
                            {"interface-name": "HundredGigE0/0/0/23.851"},
                        ]
                    }
                }
            }
        },
    }
    physical_edges = [
        {
            "local": {
                "device": "lbnl-data-sw",
                "interface": "HundredGigE0/0/0/0.2401",
            }
        },
        {
            "local": {
                "device": "uky-data-sw",
                "interface": "HundredGigE0/0/0/23.851",
            }
        },
    ]
    client = FakeClient(
        {
            ("get_device_config", "lbnl-data-sw"): lbnl_config,
            ("get_device_config", "uky-data-sw"): uky_config,
            (
                "check_isis_adjacencies",
                "lbnl-data-sw",
            ): {
                "status": "success",
                "data": {
                    "device": "lbnl-data-sw",
                    "adjacencies": [
                        {
                            "system_id": "uky-data-sw",
                            "interface": "Hu0/0/0/0.2401",
                            "state": "Up",
                        }
                    ],
                },
            },
            (
                "check_isis_adjacencies",
                "uky-data-sw",
            ): {
                "status": "success",
                "data": {
                    "device": "uky-data-sw",
                    "adjacencies": [
                        {
                            "system_id": "lbnl-data-sw",
                            "interface": "Hu0/0/0/23.851",
                            "state": "Up",
                        }
                    ],
                },
            },
        }
    )

    async def call_mcp(client_arg, tool, params=None):
        from agent.mcp_client import _tool_data

        result = await client_arg.call_tool(tool, {"params": params or {}})
        return _tool_data(result)

    import nso_facts.topology.underlay as underlay_mod

    original = underlay_mod.call_mcp
    underlay_mod.call_mcp = call_mcp
    try:
        static_edges, issues, _ = await collect_static_underlay(
            client,
            ["lbnl-data-sw", "uky-data-sw"],
            physical_edges=physical_edges,
        )
    finally:
        underlay_mod.call_mcp = original

    assert len(static_edges) == 1
    assert static_edges[0]["local"]["interface"] == "HundredGigE0/0/0/0.2401"
    assert static_edges[0]["remote"]["interface"] == "HundredGigE0/0/0/23.851"
    assert not any(i["code"] == "live_adjacency_not_in_config" for i in issues)


@pytest.mark.asyncio
async def test_collect_static_and_operational_underlay():
    client = FakeClient(
        {
            ("get_device_config", "lbnl-data-sw"): IOS_XR_ISIS_CONFIG,
            ("get_device_config", "renc-data-sw"): IOS_XR_ISIS_CONFIG,
            ("check_isis_adjacencies", "lbnl-data-sw"): ISIS_ADJ_LBNL,
            ("check_isis_adjacencies", "renc-data-sw"): ISIS_ADJ_RENC,
        }
    )

    async def call_mcp(client_arg, tool, params=None):
        from agent.mcp_client import _tool_data

        result = await client_arg.call_tool(tool, {"params": params or {}})
        return _tool_data(result)

    import nso_facts.topology.underlay as underlay_mod

    original = underlay_mod.call_mcp
    underlay_mod.call_mcp = call_mcp
    try:
        static_edges, _, _ = await collect_static_underlay(
            client, ["lbnl-data-sw", "renc-data-sw"]
        )
        op_edges, issues, _ = await collect_operational_underlay(
            client, ["lbnl-data-sw", "renc-data-sw"], static_edges
        )
    finally:
        underlay_mod.call_mcp = original

    assert len(static_edges) == 1
    assert static_edges[0]["type"] == "isis_adjacency"
    assert len(op_edges) == 1
    assert op_edges[0]["state"]["status"] == "up"
    assert not any(i["code"] == "configured_no_adjacency" for i in issues)
