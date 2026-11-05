"""A device whose IS-IS read fails and then answers in the same scan: the later reading is the one kept."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from diagnostic_mas.case import Budget, CaseFile
from nso_facts.fleet_rollups import merged_edges
from nso_facts.topology.graph import isis_edge_id
from test_topology_underlay import (
    IOS_XR_ISIS_CONFIG,
    ISIS_ADJ_LBNL,
    ISIS_ADJ_RENC,
    ISIS_RENC_UNREACHABLE,
    FakeClient,
    _handler_key,
    _wrap,
)

LBNL, RENC = "lbnl-data-sw", "renc-data-sw"
EDGE = isis_edge_id(LBNL, "HundredGigE0/0/0/0.2402", RENC, "HundredGigE0/0/0/0.2400")
HANDLERS = {
    ("get_device_config", LBNL): IOS_XR_ISIS_CONFIG,
    ("get_device_config", RENC): IOS_XR_ISIS_CONFIG,
    ("check_isis_adjacencies", LBNL): ISIS_ADJ_LBNL,
    ("check_isis_adjacencies", RENC): ISIS_ADJ_RENC,
}


class FailsFirst(FakeClient):
    """Answers each call in ``first`` once with its given result, then as ``handlers`` say."""

    def __init__(self, handlers, first):
        super().__init__(handlers)
        self.first = dict(first)

    async def call_tool(self, tool, payload):
        key = (tool, _handler_key(tool, payload.get("params") or {}))
        if key in self.first:
            return _wrap(self.first.pop(key))
        return await super().call_tool(tool, payload)


async def _spine(client):
    import nso_facts.topology.underlay as underlay_mod
    from diagnostic_mas.roles.isis import run_isis_spine

    async def call_mcp(client_arg, tool, params=None):
        from nso_facts.mcp_client import _tool_data

        return _tool_data(await client_arg.call_tool(tool, {"params": params or {}}))

    with patch.object(underlay_mod, "call_mcp", call_mcp):
        return await run_isis_spine(client, [LBNL, RENC], [])


@pytest.mark.asyncio
async def test_a_device_read_on_the_second_pass_gets_its_two_sided_adjacency():
    spine = await _spine(FailsFirst(HANDLERS, {("check_isis_adjacencies", RENC): ISIS_RENC_UNREACHABLE}))

    assert [edge["id"] for edge in spine["static_edges"]] == [EDGE]
    assert [(edge["id"], edge["state"]["status"]) for edge in spine["operational_edges"]] == [(EDGE, "up")]
    layers = {"underlay": {"edges": spine["operational_edges"]}}
    assert [edge["id"] for edge in merged_edges(layers, {"underlay": {"edges": spine["static_edges"]}},
                                                "underlay")] == [EDGE]


@pytest.mark.asyncio
async def test_the_first_failure_is_kept_apart_once_the_second_pass_reads_the_device():
    spine = await _spine(FailsFirst(HANDLERS, {("check_isis_adjacencies", RENC): ISIS_RENC_UNREACHABLE}))

    assert not any("check_isis_adjacencies failed" in issue["message"] for issue in spine["issues"])
    assert not any(issue["code"] == "unexpected_live_object" for issue in spine["issues"])
    assert [issue["message"].split(":", 1)[0] for issue in spine["failed_then_read"]] == [RENC]


@pytest.mark.asyncio
async def test_a_device_neither_pass_could_read_stays_unread():
    handlers = {**HANDLERS, ("check_isis_adjacencies", RENC): ISIS_RENC_UNREACHABLE}

    spine = await _spine(FakeClient(handlers))

    assert [edge["remote"]["interface"] for edge in spine["static_edges"]] == ["?"]
    assert any(issue["message"].startswith(f"{RENC}: check_isis_adjacencies failed") for issue in spine["issues"])
    assert spine["failed_then_read"] == []


@pytest.mark.asyncio
async def test_the_scan_keeps_the_failure_the_second_pass_made_good_with_the_isis_evidence():
    failure = {"code": "collection_error", "layer": "underlay", "severity": "medium", "edge_id": None,
               "message": f"{RENC}: check_isis_adjacencies failed: RESTCONF 500: Failed to authenticate "
                          f"towards device {RENC}: SSH host key mismatch"}
    spine = {"static_summary": {}, "operational_summary": {}, "issues": [], "static_edges": [],
             "operational_edges": [], "coverage": {}, "failed_then_read": [failure]}
    case = CaseFile(budget=Budget(0, 0))
    from diagnostic_mas.coordinator import run_mandatory_spines

    with (patch("diagnostic_mas.coordinator.collect_static_physical", new=AsyncMock(return_value=([], [], {}))),
          patch("diagnostic_mas.coordinator.run_isis_spine", new=AsyncMock(return_value=spine))):
        await run_mandatory_spines(object(), object(), case, [RENC], run_bgp=False, run_service=False)

    isis = next(ev for ev in case.evidence if ev.get("role") == "isis")
    assert isis["payload"]["extra"]["failed_then_read"] == [failure]
    assert not [issue for issue in case.issues if issue.get("code") == "collection_error"]
