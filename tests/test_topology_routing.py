"""Tests for routing (BGP) topology collectors."""

from __future__ import annotations

from typing import Any

import pytest

from agent.topology.graph import bgp_edge_id
from agent.topology.routing import (
    BgpNeighborConfig,
    BgpSessionObservation,
    collect_operational_routing,
    collect_static_routing,
    pair_bgp_observations,
    parse_bgp_summary_text,
    parse_configured_bgp,
    resolve_neighbor_device,
)

IOS_XR_BGP_CONFIG = {
    "status": "success",
    "data": {
        "tailf-ned-cisco-ios-xr:bgp": {
            "bgp-no-instance": [
                {
                    "id": 398900,
                    "router-id": "10.0.0.1",
                    "neighbor": [
                        {"id": "10.0.0.2", "remote-as": 398900},
                    ],
                }
            ]
        }
    },
}

BGP_SUMMARY_LBNL = """\
BGP router identifier 10.129.0.1, local AS number 398900
Neighbor            V    AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  St/PfxRcd
10.128.128.1        4 398900 1003029 1002987  6408872    0    0    1w6d 200160 (Estab)
10.148.0.1          4 398900       0       0        0    0    0 00:00:00 Idle
"""

BGP_SUMMARY_UKY = """\
BGP router identifier 10.128.128.1, local AS number 398900
Neighbor            V    AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  St/PfxRcd
10.129.0.1        4 398900 1003029 1002987  6408872    0    0    1w6d 200160 (Estab)
"""

IOS_XR_BGP_CONFIG_LBNL = {
    "status": "success",
    "data": {
        "tailf-ned-cisco-ios-xr:bgp": {
            "bgp-no-instance": [
                {
                    "id": 398900,
                    "router-id": "10.129.0.1",
                    "neighbor": [
                        {"id": "10.128.128.1", "remote-as": 398900},
                        {"id": "10.148.0.1", "remote-as": 398900},
                    ],
                }
            ]
        }
    },
}

IOS_XR_BGP_CONFIG_UKY = {
    "status": "success",
    "data": {
        "tailf-ned-cisco-ios-xr:bgp": {
            "bgp-no-instance": [
                {
                    "id": 398900,
                    "router-id": "10.128.128.1",
                    "neighbor": [
                        {"id": "10.129.0.1", "remote-as": 398900},
                    ],
                }
            ]
        }
    },
}

IOS_XR_BGP_CONFIG_B = {
    "status": "success",
    "data": {
        "tailf-ned-cisco-ios-xr:bgp": {
            "bgp-no-instance": [
                {
                    "id": 398900,
                    "router-id": "10.0.0.2",
                    "neighbor": [
                        {"id": "10.0.0.1", "remote-as": 398900},
                    ],
                }
            ]
        }
    },
}

BGP_SUMMARY_A = """\
BGP router identifier 10.0.0.1, local AS number 398900
Neighbor                     Spk    AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  St/PfxRcd
10.0.0.2                       0 398900    1000    1000        0    0    0 1w2d     Established
"""

BGP_SUMMARY_B = """\
BGP router identifier 10.0.0.2, local AS number 398900
Neighbor                     Spk    AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  St/PfxRcd
10.0.0.1                       0 398900    1000    1000        0    0    0 1w2d     Established
"""


def test_bgp_edge_id_orders_addresses():
    assert (
        bgp_edge_id("b", "10.0.0.2", "a", "10.0.0.1")
        == "bgp:10.0.0.1:10.0.0.2:a:b"
    )


def test_parse_configured_bgp_from_device_config_envelope():
    """get_device_config nests YANG under data.config."""
    yang = IOS_XR_BGP_CONFIG["data"]
    envelope = {
        "status": "success",
        "data": {"device": "lbnl-data-sw", "config": yang},
    }
    neighbors, router_id = parse_configured_bgp(envelope, device="lbnl-data-sw")
    assert router_id == "10.0.0.1"
    assert [n.neighbor_address for n in neighbors] == ["10.0.0.2"]


def test_parse_configured_bgp_ios_xr():
    neighbors, router_id = parse_configured_bgp(IOS_XR_BGP_CONFIG, device="lbnl-data-sw")
    assert router_id == "10.0.0.1"
    assert len(neighbors) == 1
    assert neighbors[0].neighbor_address == "10.0.0.2"
    assert neighbors[0].remote_as == 398900


def test_parse_bgp_summary_text_ios_xr_estab():
    observations = parse_bgp_summary_text("lbnl-data-sw", BGP_SUMMARY_LBNL)
    by_neighbor = {obs.neighbor_address: obs.state for obs in observations}
    assert by_neighbor["10.128.128.1"] == "Estab"
    assert by_neighbor["10.148.0.1"] == "Idle"


def test_parse_bgp_summary_text_numeric_pfxrcd_is_established():
    """XR often prints prefix count in St/PfxRcd instead of 'Established'."""
    text = """\
BGP router identifier 10.128.128.1, local AS number 398900
Neighbor        Spk    AS MsgRcvd MsgSent   TblVer  InQ OutQ  Up/Down  St/PfxRcd
10.128.0.1        0 398900  534302 142271230 200520256    0    0    5d16h          0
10.129.0.1        0 398900  530583 140673049 200520256    0    0     1w5d          0
10.133.0.1        0 398900 152987298 141753156 200520256    0    0     1w5d     445667
10.148.0.1        0 398900       0       0        0    0    0 00:00:00 Idle
"""
    observations = parse_bgp_summary_text("uky-data-sw", text)
    by_neighbor = {obs.neighbor_address: obs.state for obs in observations}
    assert by_neighbor["10.128.0.1"] == "Established"
    assert by_neighbor["10.129.0.1"] == "Established"
    assert by_neighbor["10.133.0.1"] == "Established"
    assert by_neighbor["10.148.0.1"] == "Idle"


def test_parse_bgp_summary_text():
    observations = parse_bgp_summary_text("lbnl-data-sw", BGP_SUMMARY_A)
    assert len(observations) == 1
    assert observations[0].neighbor_address == "10.0.0.2"
    assert observations[0].state == "Established"


def test_resolve_neighbor_device_by_router_id():
    router_ids = {"lbnl-data-sw": "10.0.0.1", "renc-data-sw": "10.0.0.2"}
    assert (
        resolve_neighbor_device("10.0.0.2", {}, router_ids) == "renc-data-sw"
    )


def test_pair_bgp_observations_bidirectional_up():
    observations = [
        BgpSessionObservation("lbnl-data-sw", "10.0.0.2", "Established"),
        BgpSessionObservation("renc-data-sw", "10.0.0.1", "Established"),
    ]
    neighbor_configs = {
        "lbnl-data-sw": [
            BgpNeighborConfig("lbnl-data-sw", "10.0.0.2", 398900, 398900)
        ],
        "renc-data-sw": [
            BgpNeighborConfig("renc-data-sw", "10.0.0.1", 398900, 398900)
        ],
    }
    router_ids = {"lbnl-data-sw": "10.0.0.1", "renc-data-sw": "10.0.0.2"}
    edges, issues = pair_bgp_observations(
        observations, neighbor_configs, router_ids
    )
    assert len(edges) == 1
    assert edges[0]["state"]["status"] == "up"
    assert not any(i["code"] == "one_sided_session" for i in issues)


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
    if tool == "explore_nso_path":
        return params.get("path", "")
    if tool in ("exec_show", "get_device_config", "check_isis_adjacencies"):
        if tool == "exec_show":
            return f"{params.get('device_name')}:{params.get('input_command')}"
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
async def test_collect_static_and_operational_routing():
    client = FakeClient(
        {
            ("get_device_config", "lbnl-data-sw"): IOS_XR_BGP_CONFIG,
            ("get_device_config", "renc-data-sw"): IOS_XR_BGP_CONFIG_B,
            (
                "exec_show",
                "lbnl-data-sw:bgp ipv4 unicast summary",
            ): {"status": "success", "data": {"result": BGP_SUMMARY_A}},
            (
                "exec_show",
                "renc-data-sw:bgp ipv4 unicast summary",
            ): {"status": "success", "data": {"result": BGP_SUMMARY_B}},
        }
    )

    async def call_mcp(client_arg, tool, params=None):
        from agent.mcp_client import _tool_data

        result = await client_arg.call_tool(tool, {"params": params or {}})
        return _tool_data(result)

    import agent.topology.routing as routing_mod

    original = routing_mod.call_mcp
    routing_mod.call_mcp = call_mcp
    try:
        static_edges, _, _ = await collect_static_routing(
            client, ["lbnl-data-sw", "renc-data-sw"]
        )
        op_edges, issues, _ = await collect_operational_routing(
            client, ["lbnl-data-sw", "renc-data-sw"], static_edges
        )
    finally:
        routing_mod.call_mcp = original

    assert len(static_edges) == 1
    assert static_edges[0]["type"] == "bgp_session"
    assert len(op_edges) == 1
    assert op_edges[0]["state"]["status"] == "up"
    assert not any(i["code"] == "configured_no_session" for i in issues)


@pytest.mark.asyncio
async def test_collect_static_lbnl_uky_from_config_and_live():
    """Lab mesh: config pairs LBNL↔UKY; live summary uses IOS-XR (Estab) format."""
    client = FakeClient(
        {
            ("get_device_config", "lbnl-data-sw"): IOS_XR_BGP_CONFIG_LBNL,
            ("get_device_config", "uky-data-sw"): IOS_XR_BGP_CONFIG_UKY,
            (
                "exec_show",
                "lbnl-data-sw:bgp ipv4 unicast summary",
            ): {"status": "success", "data": {"result": BGP_SUMMARY_LBNL}},
            (
                "exec_show",
                "uky-data-sw:bgp ipv4 unicast summary",
            ): {"status": "success", "data": {"result": BGP_SUMMARY_UKY}},
        }
    )

    async def call_mcp(client_arg, tool, params=None):
        from agent.mcp_client import _tool_data

        result = await client_arg.call_tool(tool, {"params": params or {}})
        return _tool_data(result)

    import agent.topology.routing as routing_mod

    original = routing_mod.call_mcp
    routing_mod.call_mcp = call_mcp
    try:
        static_edges, _, _ = await collect_static_routing(
            client, ["lbnl-data-sw", "uky-data-sw"]
        )
        op_edges, issues, _ = await collect_operational_routing(
            client, ["lbnl-data-sw", "uky-data-sw"], static_edges
        )
    finally:
        routing_mod.call_mcp = original

    assert len(static_edges) == 1
    assert static_edges[0]["id"] == (
        "bgp:10.128.128.1:10.129.0.1:uky-data-sw:lbnl-data-sw"
    )
    assert len(op_edges) == 1
    assert op_edges[0]["state"]["status"] == "up"
    assert not any(i["code"] == "missing_reverse_session" for i in issues)


def test_exec_show_plain_string_response_parses():
    from agent.topology.routing import _exec_show_text, parse_bgp_summary_text

    text = _exec_show_text(BGP_SUMMARY_LBNL)
    observations = parse_bgp_summary_text("lbnl-data-sw", text)
    assert any(obs.neighbor_address == "10.128.128.1" for obs in observations)


@pytest.mark.asyncio
async def test_exec_show_mcp_returns_bare_string():
    """MCP _tool_data may return exec output as a plain string, not {result: ...}."""
    client = FakeClient(
        {
            ("get_device_config", "lbnl-data-sw"): IOS_XR_BGP_CONFIG_LBNL,
            ("get_device_config", "uky-data-sw"): IOS_XR_BGP_CONFIG_UKY,
            (
                "exec_show",
                "lbnl-data-sw:bgp ipv4 unicast summary",
            ): BGP_SUMMARY_LBNL,
            (
                "exec_show",
                "uky-data-sw:bgp ipv4 unicast summary",
            ): BGP_SUMMARY_UKY,
        }
    )

    async def call_mcp(client_arg, tool, params=None):
        from agent.mcp_client import _tool_data

        result = await client_arg.call_tool(tool, {"params": params or {}})
        return _tool_data(result)

    import agent.topology.routing as routing_mod

    original = routing_mod.call_mcp
    routing_mod.call_mcp = call_mcp
    try:
        _, issues, _ = await collect_operational_routing(
            client, ["lbnl-data-sw", "uky-data-sw"], []
        )
    finally:
        routing_mod.call_mcp = original

    assert not any(i["code"] == "missing_reverse_session" for i in issues)
