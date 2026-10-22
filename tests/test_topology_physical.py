"""Tests for physical layer collectors and parsers."""

from __future__ import annotations

from typing import Any

import pytest

from nso_facts.topology.physical import (
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
    if tool == "get_device_config":
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
async def test_static_interfaces_prefer_get_device_config(monkeypatch):
    """Config-first: one get_device_config; no explore ladder when config parses."""
    from nso_facts.topology import physical as physical_mod

    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_call_mcp(client, tool, params=None):
        calls.append((tool, params))
        assert tool == "get_device_config"
        return IOS_XR_INTERFACE_CONFIG

    monkeypatch.setattr(physical_mod, "call_mcp", fake_call_mcp)
    edges = await physical_mod._static_interfaces_for_device(object(), "sw1")
    assert [e["local"]["interface"] for e in edges] == [
        "HundredGigE0/0/0/0",
        "Loopback0",
    ]
    assert calls == [("get_device_config", {"device_name": "sw1"})]
    assert not any(t == "explore_nso_path" for t, _ in calls)


@pytest.mark.asyncio
async def test_static_interfaces_explore_fallback_when_config_empty(monkeypatch):
    from nso_facts.topology import physical as physical_mod

    calls: list[str] = []

    async def fake_call_mcp(client, tool, params=None):
        calls.append(tool)
        if tool == "get_device_config":
            return {"status": "success", "data": {"config": {}}}
        if tool == "explore_nso_path":
            path = (params or {}).get("path", "")
            if path.endswith("/config") and (params or {}).get("depth") == 1:
                return {
                    "status": "success",
                    "data": {"tailf-ned-cisco-ios-xr:interface": {}},
                }
            if "tailf-ned-cisco-ios-xr:interface" in path:
                return IOS_XR_INTERFACE_CONFIG
            return {"status": "success", "data": {}}
        raise AssertionError(f"unexpected {tool} {params}")

    monkeypatch.setattr(physical_mod, "call_mcp", fake_call_mcp)
    edges = await physical_mod._static_interfaces_for_device(object(), "sw1")
    assert len(edges) == 2
    assert calls[0] == "get_device_config"
    assert "explore_nso_path" in calls


@pytest.mark.asyncio
async def test_collect_static_physical_builds_edges():
    client = FakeClient(
        {
            ("get_device_config", "sw1"): IOS_XR_INTERFACE_CONFIG,
        }
    )

    async def call_mcp(client_arg, tool, params=None):
        from nso_facts.mcp_client import _tool_data

        result = await client_arg.call_tool(tool, {"params": params or {}})
        return _tool_data(result)

    import nso_facts.topology.physical as physical_mod

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
        from nso_facts.mcp_client import _tool_data

        result = await client_arg.call_tool(tool, {"params": params or {}})
        return _tool_data(result)

    import nso_facts.topology.physical as physical_mod

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
        from nso_facts.mcp_client import _tool_data

        result = await client_arg.call_tool(tool, {"params": params or {}})
        return _tool_data(result)

    import nso_facts.topology.physical as physical_mod

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
        from nso_facts.mcp_client import _tool_data

        result = await client_arg.call_tool(tool, {"params": params or {}})
        return _tool_data(result)

    import nso_facts.topology.physical as physical_mod

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


# A Ciena device has no CLI exec action: the MCP server says so with exec_unavailable.
EXEC_UNAVAILABLE = {"status": "error", "error_code": "exec_unavailable",
                    "error_message": "device 'cien-data-sw' exists but has no CLI exec action"}
CIENA_HEALTH = {"status": "success", "data": {"device": "cien-data-sw", "platform": "saos", "interfaces": [
    {"name": "14", "admin_status": "up", "oper_status": "up", "read_ok": True},
    {"name": "1", "admin_status": "up", "oper_status": "down", "read_ok": True},
    {"name": "3", "admin_status": "down", "oper_status": "down", "read_ok": True},
    {"name": "m1", "admin_status": "up", "oper_status": None, "read_ok": True},
]}}


def _iface_edges(device: str, names: list[str]) -> list[dict[str, Any]]:
    return [{"id": f"if:{device}:{n}", "type": "interface", "local": {"device": device, "interface": n},
             "remote": None} for n in names]


async def _operational(handlers: dict, static_edges: list, monkeypatch) -> tuple[dict, list]:
    from nso_facts.mcp_client import _tool_data
    import nso_facts.topology.physical as physical_mod

    client = FakeClient(handlers)

    async def call_mcp(client_arg, tool, params=None):
        return _tool_data(await client_arg.call_tool(tool, {"params": params or {}}))

    monkeypatch.setattr(physical_mod, "call_mcp", call_mcp)
    edges, issues, _ = await collect_operational_physical(client, static_edges)
    return {edge["id"]: edge["state"] for edge in edges}, issues


@pytest.mark.asyncio
async def test_device_without_cli_exec_reads_interface_states_from_get_interface_health(monkeypatch):
    states, issues = await _operational(
        {("exec_show", "cien-data-sw"): EXEC_UNAVAILABLE, ("get_interface_health", "cien-data-sw"): CIENA_HEALTH},
        _iface_edges("cien-data-sw", ["14", "1", "3", "m1"]), monkeypatch)

    assert {edge: state["status"] for edge, state in states.items()} == {
        "if:cien-data-sw:14": "up", "if:cien-data-sw:1": "down",
        "if:cien-data-sw:3": "admin-down", "if:cien-data-sw:m1": "unknown"}
    # A port the device gives no state for is unknown, not down, and not a config mismatch.
    assert "oper" not in states["if:cien-data-sw:m1"] and states["if:cien-data-sw:m1"]["no_state"] is True
    assert not [i for i in issues if i["code"] in ("collection_error", "config_live_mismatch")]


@pytest.mark.asyncio
async def test_other_exec_failures_and_a_failed_fallback_leave_interfaces_unknown(monkeypatch):
    locked = {"status": "error", "error_code": "nso_error", "error_message": "Device brist-data-sw is southbound locked"}
    states, issues = await _operational(
        {("exec_show", "brist-data-sw"): locked,
         ("exec_show", "cien-data-sw"): EXEC_UNAVAILABLE,
         ("get_interface_health", "cien-data-sw"): {"status": "error", "error_code": "timeout", "error_message": "too slow"}},
        _iface_edges("brist-data-sw", ["HundredGigE0/0/0/1"]) + _iface_edges("cien-data-sw", ["14"]), monkeypatch)

    assert {state["status"] for state in states.values()} == {"unknown"}
    # The whole device went unread: no interface of it is a naming mismatch.
    assert all(state.get("not_read") is True for state in states.values())
    messages = sorted(i["message"] for i in issues if i["code"] == "collection_error")
    assert messages[0] == "brist-data-sw: Device brist-data-sw is southbound locked"
    assert messages[1].startswith("cien-data-sw: ") and "too slow" in messages[1]
