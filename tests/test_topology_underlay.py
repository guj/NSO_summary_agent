"""Tests for underlay (IS-IS) topology collectors."""

from __future__ import annotations

from typing import Any

import pytest

from nso_facts.topology.graph import isis_edge_id
from nso_facts.topology.underlay import (
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


def test_a_system_id_names_a_device_it_extends_not_one_it_is_part_of():
    devices = ["eduky-data-sw", "cien-data-sw"]
    assert resolve_system_id("cien-data-sw-8190", devices) == "cien-data-sw"
    assert resolve_system_id("uky-data-sw", devices) is None


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
        from nso_facts.mcp_client import _tool_data

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
        from nso_facts.mcp_client import _tool_data

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


NEWY_SEES_PRIN = [
    AdjacencyObservation("newy-data-sw", "HundredGigE0/0/0/20.3000", "prin-data-sw", "Up")
]
ONE_SIDED = "isis:newy-data-sw:HundredGigE0/0/0/20.3000:prin-data-sw:?"


def test_pair_keeps_a_one_sided_reading_when_the_far_end_was_not_read():
    devices = ["newy-data-sw", "prin-data-sw"]
    edges, issues = pair_adjacency_observations(NEWY_SEES_PRIN, devices, unread_devices={"prin-data-sw"})

    assert edges == [{
        "id": ONE_SIDED,
        "type": "isis_adjacency",
        "local": {"device": "newy-data-sw", "interface": "HundredGigE0/0/0/20.3000"},
        "remote": {"device": "prin-data-sw", "interface": "?"},
        "state": {"local": "up", "remote": "unknown", "status": "unknown",
                  "detail": "far end not read this scan"},
    }]
    assert issues == []
    # A far end that answered but does not list the adjacency is not "not read": its silence is a reading.
    [edge] = pair_adjacency_observations(NEWY_SEES_PRIN, devices)[0]
    assert edge["state"] == {"local": "up", "remote": "not listed", "status": "unidirectional"}


def test_adjacency_states_follow_their_endpoints():
    devices = ["amst-data-sw", "wash-data-sw"]
    wash = AdjacencyObservation("wash-data-sw", "Hu0/0/0/1.1", "amst-data-sw", "Up")
    amst = AdjacencyObservation("amst-data-sw", "Hu0/0/0/2.1", "wash-data-sw", "Init")

    # amst sorts first, so wash's reading belongs to the remote end.
    one, _ = pair_adjacency_observations([wash], devices, unread_devices={"amst-data-sw"})
    assert one[0]["local"]["device"] == "amst-data-sw"
    assert (one[0]["state"]["local"], one[0]["state"]["remote"]) == ("unknown", "up")

    two, issues = pair_adjacency_observations([wash, amst], devices)
    assert (two[0]["state"]["local"], two[0]["state"]["remote"]) == ("init", "up")
    assert [i["message"] for i in issues] == [
        "IS-IS adjacency down: amst-data-sw HundredGigE0/0/0/2.1 lists it as Init; "
        "wash-data-sw HundredGigE0/0/0/1.1 lists it as Up"]


def test_parallel_adjacencies_between_two_routers_pair_each_reading_once():
    devices = ["cern-data-sw", "wash-data-sw"]
    vlans = ("2040", "2050", "3731")
    observations = [AdjacencyObservation("cern-data-sw", f"FourHundredGigE0/0/0/26.{vlan}", "wash-data-sw", "Up")
                    for vlan in vlans]
    observations += [AdjacencyObservation("wash-data-sw", f"HundredGigE0/0/0/19.{vlan}", "cern-data-sw", "Up")
                     for vlan in vlans]

    edges, issues = pair_adjacency_observations(observations, devices)

    assert [edge["id"] for edge in edges] == [
        isis_edge_id("cern-data-sw", f"FourHundredGigE0/0/0/26.{vlan}", "wash-data-sw", f"HundredGigE0/0/0/19.{vlan}")
        for vlan in vlans]
    assert issues == []


def test_a_neighbor_outside_nso_does_not_pair_with_a_device_whose_name_contains_its_own():
    devices = ["eduky-data-sw", "star-data-sw"]
    observations = [
        AdjacencyObservation("eduky-data-sw", "HundredGigE0/0/0/0.857", "star-data-sw", "Up"),
        AdjacencyObservation("star-data-sw", "HundredGigE0/0/0/21.856", "uky-data-sw", "Up"),
        AdjacencyObservation("star-data-sw", "HundredGigE0/0/0/21.1857", "eduky-data-sw", "Up"),
    ]

    edges, issues = pair_adjacency_observations(observations, devices)

    assert [(edge["id"], edge["state"]["status"]) for edge in edges] == [
        (isis_edge_id("eduky-data-sw", "HundredGigE0/0/0/0.857", "star-data-sw", "HundredGigE0/0/0/21.1857"), "up")]
    assert [issue["code"] for issue in issues] == ["unknown_neighbor_system_id"]


ISIS_RENC_UNREACHABLE = {
    "status": "error",
    "error_message": "RESTCONF 500: Failed to connect to device renc-data-sw: connection refused: "
                     "NEDCOM CONNECT: Connect timed out in new state",
}


async def _collect(handlers: dict, static_edges: list | None = None):
    import nso_facts.topology.underlay as underlay_mod

    async def call_mcp(client_arg, tool, params=None):
        from nso_facts.mcp_client import _tool_data

        return _tool_data(await client_arg.call_tool(tool, {"params": params or {}}))

    client = FakeClient(handlers)
    devices = ["lbnl-data-sw", "renc-data-sw"]
    original = underlay_mod.call_mcp
    underlay_mod.call_mcp = call_mcp
    try:
        if static_edges is None:
            static_edges, _, _ = await collect_static_underlay(client, devices)
        op_edges, issues, _ = await collect_operational_underlay(client, devices, static_edges)
    finally:
        underlay_mod.call_mcp = original
    return static_edges, op_edges, issues


@pytest.mark.asyncio
async def test_unread_far_end_keeps_the_near_end_reading():
    static_edges, op_edges, issues = await _collect({
        ("get_device_config", "lbnl-data-sw"): IOS_XR_ISIS_CONFIG,
        ("get_device_config", "renc-data-sw"): IOS_XR_ISIS_CONFIG,
        ("check_isis_adjacencies", "lbnl-data-sw"): ISIS_ADJ_LBNL,
        ("check_isis_adjacencies", "renc-data-sw"): ISIS_RENC_UNREACHABLE,
    })

    edge_id = "isis:lbnl-data-sw:HundredGigE0/0/0/0.2402:renc-data-sw:?"
    assert [e["id"] for e in static_edges] == [edge_id]
    assert op_edges == [{"id": edge_id, "state": {
        "local": "up", "remote": "unknown", "status": "unknown", "detail": "far end not read this scan"}}]
    assert not any(i["code"] in {"configured_no_adjacency", "live_adjacency_not_in_config"} for i in issues)


@pytest.mark.asyncio
async def test_a_one_sided_static_edge_is_not_marked_down_once_both_ends_answer():
    one_sided = {
        "id": "isis:lbnl-data-sw:HundredGigE0/0/0/0.2402:renc-data-sw:?",
        "type": "isis_adjacency",
        "local": {"device": "lbnl-data-sw", "interface": "HundredGigE0/0/0/0.2402"},
        "remote": {"device": "renc-data-sw", "interface": "?"},
        "meta": {},
    }
    _, op_edges, issues = await _collect({
        ("check_isis_adjacencies", "lbnl-data-sw"): ISIS_ADJ_LBNL,
        ("check_isis_adjacencies", "renc-data-sw"): ISIS_ADJ_RENC,
    }, static_edges=[one_sided])

    assert [e["state"]["status"] for e in op_edges] == ["up"]
    assert not any(i["code"] == "configured_no_adjacency" for i in issues)


# What the MCP server returns for a platform whose NED models IS-IS config only (Ciena SAOS).
ISIS_STATE_NOT_EXPOSED = {
    "status": "error",
    "error_code": "not_exposed",
    "error_message": ("device 'renc-data-sw' runs saos, whose NED models ISIS config only and does not "
                      "expose adjacency state; no ISIS adjacency data can be read from this platform"),
}


@pytest.mark.asyncio
async def test_a_platform_without_adjacency_state_is_an_unread_end_not_a_failed_read():
    from nso_facts.topology.underlay import isis_state_not_exposed

    _, op_edges, issues = await _collect({
        ("get_device_config", "lbnl-data-sw"): IOS_XR_ISIS_CONFIG,
        ("get_device_config", "renc-data-sw"): IOS_XR_ISIS_CONFIG,
        ("check_isis_adjacencies", "lbnl-data-sw"): ISIS_ADJ_LBNL,
        ("check_isis_adjacencies", "renc-data-sw"): ISIS_STATE_NOT_EXPOSED,
    })

    # The near end's reading still stands for the adjacency.
    assert op_edges == [{"id": "isis:lbnl-data-sw:HundredGigE0/0/0/0.2402:renc-data-sw:?", "state": {
        "local": "up", "remote": "unknown", "status": "unknown", "detail": "far end not read this scan"}}]
    [gap] = [i for i in issues if i["code"] == "collection_error"]
    assert gap["severity"] == "low"
    assert gap["message"] == ("renc-data-sw: IS-IS adjacency state is not exposed on this platform ("
                              + ISIS_STATE_NOT_EXPOSED["error_message"] + ")")
    assert isis_state_not_exposed(gap)
    failed = {"code": "collection_error", "layer": "underlay",
              "message": "renc-data-sw: check_isis_adjacencies failed: RESTCONF 500: Connect timed out"}
    assert not isis_state_not_exposed(failed)


# ``show isis adjacency`` as the MCP server returns it under "raw".
ADJACENCY_TEXT = """
Thu Oct 29 17:04:47.441 UTC

IS-IS 1 Level-2 adjacencies:
System Id      Interface                SNPA           State Hold Changed  NSF IPv4 IPv6
                                                                               BFD  BFD
losa-data-sw   Hu0/0/0/22.3000          *PtoP*         Up    28   03:30:43 Yes None None
hawi-data-sw   Hu0/0/0/19.3380          *PtoP*         Up    22   1d10h    Yes None None
utah-data-sw   Hu0/0/0/19.3000          *PtoP*         Up    28   5w0d     Yes None None
salt-data-sw   Hu0/0/0/23.3000          *PtoP*         Init  21   not up   Yes None None

Total adjacency count: 4
"""


def test_change_times_come_from_the_changed_column_when_it_gives_the_time_of_day():
    from datetime import datetime, timezone

    from nso_facts.topology.underlay import adjacency_change_times

    read_at = datetime(2026, 10, 29, 17, 4, 47, tzinfo=timezone.utc)

    # A day or more old (1d10h, 5w0d) is rounded to the hour or day; "not up" has no change to time.
    assert adjacency_change_times(ADJACENCY_TEXT, read_at) == {
        ("losa-data-sw", "Hu0/0/0/22.3000"): "2026-10-29T13:34:04+00:00"}
    assert adjacency_change_times("", read_at) == {}


def test_change_times_count_from_the_router_s_own_clock_line():
    from datetime import datetime, timezone

    from nso_facts.topology.underlay import adjacency_change_times

    # A cached reply is used after the router answered; its clock line says when it answered.
    used_later = datetime(2026, 10, 29, 17, 6, 9, tzinfo=timezone.utc)
    assert adjacency_change_times(ADJACENCY_TEXT, used_later) == {
        ("losa-data-sw", "Hu0/0/0/22.3000"): "2026-10-29T13:34:04+00:00"}
    # A clock line from the last day of the year, used just after midnight.
    new_year = datetime(2027, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
    december = ADJACENCY_TEXT.replace("Thu Oct 29 17:04:47.441 UTC", "Thu Dec 31 23:59:58.004 UTC")
    assert adjacency_change_times(december, new_year) == {
        ("losa-data-sw", "Hu0/0/0/22.3000"): "2026-12-31T20:29:15+00:00"}
    # A router clock in another zone is not used.
    pacific = ADJACENCY_TEXT.replace("17:04:47.441 UTC", "10:04:47.441 PDT")
    assert adjacency_change_times(pacific, used_later) == {
        ("losa-data-sw", "Hu0/0/0/22.3000"): "2026-10-29T13:35:26+00:00"}


def test_an_adjacency_keeps_the_latest_change_either_end_read():
    devices = ["lbnl-data-sw", "renc-data-sw"]
    timed = [AdjacencyObservation("lbnl-data-sw", "HundredGigE0/0/0/0.2402", "renc-data-sw", "Up",
                                  "2026-10-29T13:34:04+00:00"),
             AdjacencyObservation("renc-data-sw", "HundredGigE0/0/0/0.2400", "lbnl-data-sw", "Up",
                                  "2026-10-29T13:34:06+00:00")]

    [edge] = pair_adjacency_observations(timed, devices)[0]
    assert edge["changed_at"] == "2026-10-29T13:34:06+00:00"
    # One end read: its own change time.
    [edge] = pair_adjacency_observations(timed[:1], devices, unread_devices={"renc-data-sw"})[0]
    assert edge["changed_at"] == "2026-10-29T13:34:04+00:00"
    # No change time read: none recorded.
    untimed = [AdjacencyObservation(obs.local_device, obs.local_interface, obs.neighbor_system_id, obs.state)
               for obs in timed]
    [edge] = pair_adjacency_observations(untimed, devices)[0]
    assert "changed_at" not in edge


@pytest.mark.asyncio
async def test_the_scan_keeps_each_adjacency_s_change_time_with_its_state():
    from datetime import datetime, timedelta, timezone

    def reply(base: dict, row: str) -> dict:
        return {**base, "data": {**base["data"], "raw": "System Id  Interface  SNPA  State Hold Changed\n" + row}}

    before = datetime.now(timezone.utc)
    _, op_edges, _ = await _collect({
        ("get_device_config", "lbnl-data-sw"): IOS_XR_ISIS_CONFIG,
        ("get_device_config", "renc-data-sw"): IOS_XR_ISIS_CONFIG,
        ("check_isis_adjacencies", "lbnl-data-sw"): reply(
            ISIS_ADJ_LBNL, "renc-data-sw  HundredGigE0/0/0/0.2402  *PtoP*  Up  27  01:00:00 Yes None None"),
        ("check_isis_adjacencies", "renc-data-sw"): reply(
            ISIS_ADJ_RENC, "lbnl-data-sw  HundredGigE0/0/0/0.2400  *PtoP*  Up  27  01:00:05 Yes None None"),
    })
    after = datetime.now(timezone.utc)

    [edge] = op_edges
    # The later of the two ends' changes: lbnl's, an hour before it was read.
    changed = datetime.fromisoformat(edge["changed_at"])
    assert before - timedelta(hours=1, seconds=1) <= changed <= after - timedelta(hours=1)
