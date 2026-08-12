"""Tests for physical layer collectors and parsers."""

from __future__ import annotations

from typing import Any

import pytest

from agent.topology.physical import (
    parse_configured_interface_names,
    parse_interfaces_brief,
    parse_interfaces_summary,
    collect_operational_physical,
    collect_static_physical,
    _classify_interface_status,
)


IOS_XR_INTERFACE_CONFIG = {
    "status": "success",
    "data": {
        "tailf-ned-cisco-ios-xr:interface": {
            "interface": [
                {"name": "Loopback0"},
                {"name": "HundredGigE0/0/0/0"},
            ]
        }
    },
}

SUMMARY_TEXT = (
    "Interface                    Admin   Oper    LineP   Encap   MTU\n"
    "Loopback0                     up      up      up      ARPA    1500\n"
    "HundredGigE0/0/0/0            up      up      up      ARPA    9216\n"
    "HundredGigE0/0/0/2            up      down    down    ARPA    9216\n"
)

BRIEF_TEXT = """
               Intf       Intf        LineP              Encap  MTU        BW
               Name       State       State               Type (byte)    (Kbps)
--------------------------------------------------------------------------------
                Lo0          up          up           Loopback  1500          0
          Hu0/0/0/0          up          up               ARPA  9216  100000000
          Hu0/0/0/2        down        down               ARPA  9216  100000000
          Hu0/0/0/3  admin-down  admin-down               ARPA  9216  100000000
          Hu0/0/0/9          up          up               ARPA  9216  100000000
             BV4002          up          up               ARPA  9216   10000000
         FH0/0/0/25        down        down               ARPA  9216  400000000
       TF0/0/0/10/0          up          up               ARPA  9216   25000000
            BV50000        down        down               ARPA  9216   10000000
"""


def test_parse_configured_interface_names_ios_xr():
    names = parse_configured_interface_names(IOS_XR_INTERFACE_CONFIG)
    assert names == ["HundredGigE0/0/0/0", "Loopback0"]


def test_parse_configured_interface_names_from_config_envelope():
    yang = IOS_XR_INTERFACE_CONFIG["data"]
    envelope = {
        "status": "success",
        "data": {"device": "lbnl-data-sw", "config": yang},
    }
    assert parse_configured_interface_names(envelope) == [
        "HundredGigE0/0/0/0",
        "Loopback0",
    ]


def test_parse_configured_interface_names_from_explore_payload():
    yang = IOS_XR_INTERFACE_CONFIG["data"]
    envelope = {
        "status": "success",
        "data": {
            "path": "tailf-ncs:devices/device=lbnl-data-sw/config",
            "depth": 2,
            "payload": yang,
        },
    }
    assert parse_configured_interface_names(envelope) == [
        "HundredGigE0/0/0/0",
        "Loopback0",
    ]


def test_parse_configured_interface_names_openconfig():
    payload = {
        "status": "success",
        "data": {
            "openconfig-interfaces:interfaces": {
                "interface": [
                    {"name": "eth0"},
                    {"name": "eth1"},
                ]
            }
        },
    }
    assert parse_configured_interface_names(payload) == ["eth0", "eth1"]


def test_parse_interfaces_summary():
    live = parse_interfaces_summary(SUMMARY_TEXT)
    assert live["Loopback0"] == {"admin": "up", "oper": "up"}
    assert live["HundredGigE0/0/0/2"]["oper"] == "down"


def test_parse_interfaces_brief():
    live = parse_interfaces_brief(BRIEF_TEXT)
    assert live["Lo0"] == {"admin": "up", "oper": "up"}
    assert live["Hu0/0/0/2"] == {"admin": "down", "oper": "down"}
    assert live["Hu0/0/0/3"] == {"admin": "admin-down", "oper": "admin-down"}
    assert live["Hu0/0/0/9"]["admin"] == "up"
    assert "Name" not in live


def test_classify_interface_status_separates_admin_down():
    assert _classify_interface_status("up", "up", False) == "up"
    assert _classify_interface_status("admin-down", "admin-down", False) == "admin-down"
    assert _classify_interface_status("up", "down", False) == "down"
    assert _classify_interface_status("down", "down", False) == "down"
    assert _classify_interface_status("up", "up", True) == "degraded"


class FakeClient:
    def __init__(self, handlers: dict[tuple[str, Any], Any]):
        self.handlers = handlers

    async def call_tool(self, tool: str, payload: dict[str, Any]):
        params = payload.get("params") or {}
        key = (tool, _handler_key(tool, params))
        if key not in self.handlers:
            raise AssertionError(f"unexpected tool call: {tool} {params}")
        return _wrap(self.handlers[key])


def _handler_key(tool: str, params: dict[str, Any]) -> str:
    if tool == "explore_nso_path":
        return params.get("path", "")
    if tool == "exec_show":
        return params.get("device_name", "")
    if tool == "get_interface_health":
        return params.get("device_name", "")
    if tool == "check_isis_adjacencies":
        return params.get("device_name", "")
    return repr(sorted(params.items()))


def _wrap(data: Any):
    class Result:
        def __init__(self, value: Any):
            self.data = value
            self.structured_content = None
            self.content = []

    return Result(data)


@pytest.mark.asyncio
async def test_collect_static_physical_builds_edges():
    config_root = {
        "status": "success",
        "data": {"tailf-ned-cisco-ios-xr:interface": {}},
    }
    client = FakeClient(
        {
            ("explore_nso_path", "tailf-ncs:devices/device=sw1/config"): config_root,
            (
                "explore_nso_path",
                "tailf-ncs:devices/device=sw1/config/tailf-ned-cisco-ios-xr:interface",
            ): IOS_XR_INTERFACE_CONFIG,
        }
    )

    async def call_mcp(client_arg, tool, params=None):
        from agent.mcp_client import _tool_data

        result = await client_arg.call_tool(tool, {"params": params or {}})
        return _tool_data(result)

    import agent.topology.physical as physical_mod

    original = physical_mod.call_mcp
    physical_mod.call_mcp = call_mcp
    try:
        edges, issues, coverage = await collect_static_physical(client, ["sw1"])
    finally:
        physical_mod.call_mcp = original

    assert coverage["devices_queried"] == 1
    assert len(edges) == 2
    assert edges[0]["type"] == "interface"
    assert not any(i["code"] == "collection_error" for i in issues)


@pytest.mark.asyncio
async def test_collect_operational_physical_marks_down_and_unexpected():
    static_edges = [
        {
            "id": "if:sw1:Loopback0",
            "type": "interface",
            "local": {"device": "sw1", "interface": "Loopback0"},
            "remote": None,
        },
        {
            "id": "if:sw1:HundredGigE0/0/0/2",
            "type": "interface",
            "local": {"device": "sw1", "interface": "HundredGigE0/0/0/2"},
            "remote": None,
        },
        {
            "id": "if:sw1:HundredGigE0/0/0/3",
            "type": "interface",
            "local": {"device": "sw1", "interface": "HundredGigE0/0/0/3"},
            "remote": None,
        },
        {
            "id": "if:sw1:HundredGigE0/0/0/9",
            "type": "interface",
            "local": {"device": "sw1", "interface": "HundredGigE0/0/0/9"},
            "remote": None,
        },
        {
            "id": "if:sw1:HundredGigE0/0/0/99",
            "type": "interface",
            "local": {"device": "sw1", "interface": "HundredGigE0/0/0/99"},
            "remote": None,
        },
    ]
    brief = {
        "status": "success",
        "data": {
            "device": "sw1",
            "command": "show interfaces brief",
            "result": BRIEF_TEXT,
        },
    }
    client = FakeClient({("exec_show", "sw1"): brief})

    async def call_mcp(client_arg, tool, params=None):
        from agent.mcp_client import _tool_data

        result = await client_arg.call_tool(tool, {"params": params or {}})
        return _tool_data(result)

    import agent.topology.physical as physical_mod

    original = physical_mod.call_mcp
    physical_mod.call_mcp = call_mcp
    try:
        edges, issues, coverage = await collect_operational_physical(client, static_edges)
    finally:
        physical_mod.call_mcp = original

    assert coverage["devices_queried"] == 1
    by_id = {edge["id"]: edge for edge in edges}
    assert by_id["if:sw1:Loopback0"]["state"]["status"] == "up"
    assert by_id["if:sw1:HundredGigE0/0/0/2"]["state"]["status"] == "down"
    assert by_id["if:sw1:HundredGigE0/0/0/3"]["state"]["status"] == "admin-down"
    assert by_id["if:sw1:HundredGigE0/0/0/9"]["state"]["status"] == "up"
    assert by_id["if:sw1:HundredGigE0/0/0/99"]["state"]["status"] == "unknown"
    assert by_id["if:sw1:HundredGigE0/0/0/99"]["state"]["detail"] == (
        "not in interfaces brief"
    )
    assert any(i["code"] == "unexpected_live_object" for i in issues)
    assert any(
        i["code"] == "config_live_mismatch" and i.get("nso") == "HundredGigE0/0/0/99"
        for i in issues
    )


@pytest.mark.asyncio
async def test_operational_physical_type_change_candidate():
    static_edges = [
        {
            "id": "if:renc:FourHundredGigE0/0/0/32",
            "type": "interface",
            "local": {
                "device": "renc",
                "interface": "FourHundredGigE0/0/0/32",
            },
            "remote": None,
        }
    ]
    brief_text = """
Interface                      Intf       LineP              Encap  MTU        BW
Hu0/0/0/32                     up         up                 ARPA   1514       100000000
"""
    brief = {
        "status": "success",
        "data": {
            "device": "renc",
            "command": "show interfaces brief",
            "result": brief_text,
        },
    }
    client = FakeClient({("exec_show", "renc"): brief})

    async def call_mcp(client_arg, tool, params=None):
        from agent.mcp_client import _tool_data

        result = await client_arg.call_tool(tool, {"params": params or {}})
        return _tool_data(result)

    import agent.topology.physical as physical_mod

    original = physical_mod.call_mcp
    physical_mod.call_mcp = call_mcp
    try:
        edges, issues, _cov = await collect_operational_physical(client, static_edges)
    finally:
        physical_mod.call_mcp = original

    assert edges[0]["state"]["status"] == "unknown"
    mismatch = next(i for i in issues if i["code"] == "config_live_mismatch")
    assert mismatch["candidates"] == ["Hu0/0/0/32"]
    assert mismatch["kind"] == "type_change"
    assert mismatch["confirmed"] is False
    assert any(i["code"] == "unexpected_live_object" for i in issues)


@pytest.mark.asyncio
async def test_operational_physical_admin_equivalence_matches():
    static_edges = [
        {
            "id": "if:renc:FourHundredGigE0/0/0/32",
            "type": "interface",
            "local": {
                "device": "renc",
                "interface": "FourHundredGigE0/0/0/32",
            },
            "remote": None,
        }
    ]
    brief_text = """
Interface                      Intf       LineP              Encap  MTU        BW
Hu0/0/0/32                     down       down               ARPA   1514       100000000
"""
    brief = {
        "status": "success",
        "data": {
            "device": "renc",
            "command": "show interfaces brief",
            "result": brief_text,
        },
    }
    client = FakeClient({("exec_show", "renc"): brief})
    equivalences = {
        "renc": {"FourHundredGigE0/0/0/32": ["Hu0/0/0/32"]},
    }

    async def call_mcp(client_arg, tool, params=None):
        from agent.mcp_client import _tool_data

        result = await client_arg.call_tool(tool, {"params": params or {}})
        return _tool_data(result)

    import agent.topology.physical as physical_mod

    original = physical_mod.call_mcp
    physical_mod.call_mcp = call_mcp
    try:
        edges, issues, _cov = await collect_operational_physical(
            client, static_edges, equivalences=equivalences
        )
    finally:
        physical_mod.call_mcp = original

    assert edges[0]["state"]["status"] == "down"
    assert not any(i["code"] == "config_live_mismatch" for i in issues)
    assert not any(i["code"] == "unexpected_live_object" for i in issues)
