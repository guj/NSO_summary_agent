"""IS-IS adjacencies that are not up at both ends: an expected one both routers now omit, and one the
two routers see differently (one lists it as Init, or does not list it at all)."""

from __future__ import annotations

import pytest

import nso_facts.topology.underlay as underlay
from diagnostic_mas.case import Budget, CaseFile, add_evidence, open_issue
from diagnostic_mas.case_delta import _routing_session_line, _routing_sessions_lost
from diagnostic_mas.operator_report import (
    format_devices_operator,
    format_followup_operator,
    format_result_line,
)
from diagnostic_mas.roles.isis import expected_adjacencies, run_isis_spine
from nso_facts.topology.graph import isis_edge_id
from nso_facts.topology.underlay import AdjacencyObservation, pair_adjacency_observations

NEWY, RUTG, PRIN = "newy-data-sw", "rutg-data-sw", "prin-data-sw"
NEWY_IF, RUTG_IF = "HundredGigE0/0/0/19.3000", "HundredGigE0/0/0/23.3000"
EDGE = isis_edge_id(NEWY, NEWY_IF, RUTG, RUTG_IF)
WAS_UP = {"id": EDGE, "local": {"device": NEWY, "interface": NEWY_IF},
          "remote": {"device": RUTG, "interface": RUTG_IF}, "held": False}


def _config(*names: str) -> dict:
    return {"status": "success", "data": {"tailf-ned-cisco-ios-xr:router": {"isis": {"interfaces": {
        "interface": [{"interface-name": name} for name in names]}}}}}


def _adjacencies(device: str, *rows: tuple[str, str]) -> dict:
    return {"status": "success", "data": {"device": device, "adjacencies": [
        {"system_id": neighbor, "interface": interface, "state": "Up"} for neighbor, interface in rows]}}


UNREACHABLE = {"status": "error", "error_message": "RESTCONF 500: Failed to connect to device: connect timed out"}
CONFIGS = {("get_device_config", NEWY): _config(NEWY_IF), ("get_device_config", RUTG): _config(RUTG_IF)}
SILENT = {("check_isis_adjacencies", NEWY): _adjacencies(NEWY), ("check_isis_adjacencies", RUTG): _adjacencies(RUTG)}


async def _spine(monkeypatch, handlers: dict, expected: list, devices=(NEWY, RUTG)) -> dict:
    async def call_mcp(client, tool, params=None):
        return handlers.get((tool, (params or {}).get("device_name")), UNREACHABLE)

    monkeypatch.setattr(underlay, "call_mcp", call_mcp)
    return await run_isis_spine(None, list(devices), [], expected_edges=expected)


def _down(spine: dict) -> list[dict]:
    return [i for i in spine["issues"] if i["code"] == "configured_no_adjacency"]


@pytest.mark.asyncio
async def test_an_adjacency_both_routers_now_omit_is_down(monkeypatch):
    spine = await _spine(monkeypatch, {**CONFIGS, **SILENT}, [WAS_UP])

    assert [e["state"] for e in spine["operational_edges"] if e["id"] == EDGE] == [{
        "local": "unknown", "remote": "unknown", "status": "down",
        "detail": "not reported by either router; both answered"}]
    [issue] = _down(spine)
    assert issue["severity"] == "high" and set(issue["devices"]) == {NEWY, RUTG}
    assert issue["message"] == (f"IS-IS adjacency down: {NEWY} {NEWY_IF} ↔ {RUTG} {RUTG_IF} "
                                "(both routers answered without it; up in the previous scan)")
    assert spine["operational_summary"]["down"] == 1


@pytest.mark.asyncio
async def test_still_missing_says_so(monkeypatch):
    spine = await _spine(monkeypatch, {**CONFIGS, **SILENT}, [{**WAS_UP, "held": True}])
    assert _down(spine)[0]["message"].endswith("(both routers answered without it; still missing since an earlier scan)")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["router_unread", "interface_in_another_adjacency", "isis_removed_from_config",
                                    "router_out_of_scope", "not_expected"])
async def test_nothing_is_claimed_without_all_the_evidence(monkeypatch, change):
    handlers, expected, devices = {**CONFIGS, **SILENT}, [WAS_UP], (NEWY, RUTG)
    if change == "router_unread":
        handlers[("check_isis_adjacencies", RUTG)] = UNREACHABLE
    elif change == "interface_in_another_adjacency":
        # rutg's end moved to another port: newy's interface has an adjacency again.
        handlers[("get_device_config", RUTG)] = _config(RUTG_IF, "HundredGigE0/0/0/24.3000")
        handlers[("check_isis_adjacencies", NEWY)] = _adjacencies(NEWY, (RUTG, NEWY_IF))
        handlers[("check_isis_adjacencies", RUTG)] = _adjacencies(RUTG, (NEWY, "HundredGigE0/0/0/24.3000"))
    elif change == "isis_removed_from_config":
        handlers[("get_device_config", RUTG)] = _config("HundredGigE0/0/0/24.3000")
    elif change == "router_out_of_scope":
        devices = (NEWY,)
    elif change == "not_expected":
        expected = []

    spine = await _spine(monkeypatch, handlers, expected, devices)
    assert _down(spine) == []
    assert not any(e["id"] == EDGE and e["state"]["status"] == "down" for e in spine["operational_edges"])


def _saved(spine: dict) -> dict:
    """The part of a saved case that the next scan reads its expectation from."""
    return {"evidence": [{"kind": "spine", "role": "isis", "payload": {
        "static_edges": spine["static_edges"], "operational_edges": spine["operational_edges"]}}]}


@pytest.mark.asyncio
async def test_the_expectation_is_carried_while_missing_and_ends_held_once_it_is_up(monkeypatch):
    missing = await _spine(monkeypatch, {**CONFIGS, **SILENT}, [WAS_UP])
    assert [(e["id"], e["held"]) for e in expected_adjacencies(_saved(missing))] == [(EDGE, True)]

    still = await _spine(monkeypatch, {**CONFIGS, **SILENT}, expected_adjacencies(_saved(missing)))
    assert "still missing" in _down(still)[0]["message"]

    back = await _spine(monkeypatch, {**CONFIGS, ("check_isis_adjacencies", NEWY): _adjacencies(NEWY, (RUTG, NEWY_IF)),
                                      ("check_isis_adjacencies", RUTG): _adjacencies(RUTG, (NEWY, RUTG_IF))},
                        expected_adjacencies(_saved(still)))
    assert _down(back) == [] and [e["state"]["status"] for e in back["operational_edges"]] == ["up"]
    assert [(e["id"], e["held"]) for e in expected_adjacencies(_saved(back))] == [(EDGE, False)]


def test_only_adjacencies_up_or_held_are_expected():
    other = isis_edge_id(NEWY, "HundredGigE0/0/0/20.3000", PRIN, "HundredGigE0/0/0/1.3000")
    saved = {"evidence": [{"kind": "spine", "role": "isis", "payload": {
        "static_edges": [{"id": EDGE, "local": WAS_UP["local"], "remote": WAS_UP["remote"], "meta": {}},
                         {"id": other, "local": {"device": NEWY, "interface": "HundredGigE0/0/0/20.3000"},
                          "remote": {"device": PRIN, "interface": "HundredGigE0/0/0/1.3000"}, "meta": {}}],
        "operational_edges": [{"id": EDGE, "state": {"status": "up"}},
                              {"id": other, "state": {"status": "down"}}]}}]}

    assert [e["id"] for e in expected_adjacencies(saved)] == [EDGE]       # a router-reported down one is not
    assert expected_adjacencies(None) == [] and expected_adjacencies({"evidence": []}) == []


def _case() -> CaseFile:
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = [NEWY, RUTG]
    open_issue(case, code="configured_no_adjacency", severity="high", layer="underlay", evidence_ids=[],
               edge_id=EDGE, devices=[NEWY, RUTG],
               message=f"IS-IS adjacency down: {NEWY} {NEWY_IF} ↔ {RUTG} {RUTG_IF} "
                       "(both routers answered without it; up in the previous scan)")
    return case


def test_the_result_line_and_follow_up_name_the_adjacency():
    assert f"1 IS-IS adjacency down: `{NEWY}` ↔ `{RUTG}`." in format_result_line(_case())
    assert "IS-IS adjacenc" not in format_result_line(CaseFile(budget=Budget(0, 0)))

    items = "\n".join(format_followup_operator(_case()))
    assert (f"Investigate 1 IS-IS adjacency down: `{NEWY}` ↔ `{RUTG}` (each router's view and port state "
            "are in its Attention line)") in items


def test_both_routers_get_an_attention_line():
    text = "\n".join(format_devices_operator(_case()))
    for device in (NEWY, RUTG):
        block = text.split(f"### {device}\n", 1)[1].split("\n### ", 1)[0]
        assert f"IS-IS adjacency down: {NEWY} {NEWY_IF} ↔ {RUTG} {RUTG_IF}" in block


def _attention(case: CaseFile, device: str) -> str:
    text = "\n".join(format_devices_operator(case))
    return text.split(f"### {device}\n", 1)[1].split("\n### ", 1)[0]


def _with_ports(states: dict[tuple[str, str], dict]) -> CaseFile:
    case = _case()
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services", "payload": {"extra": {
        "physical_operational_edges": [{"id": f"if:{device}:{interface}", "state": state}
                                       for (device, interface), state in states.items()]}}})
    return case


def test_the_attention_line_gives_each_ends_port_state():
    case = _with_ports({(NEWY, NEWY_IF): {"admin": "up", "oper": "up"},
                        (RUTG, RUTG_IF): {"admin": "down", "oper": "down"}})
    for device in (NEWY, RUTG):
        assert (f"up in the previous scan). Ports as read this scan: {NEWY} {NEWY_IF} up; "
                f"{RUTG} {RUTG_IF} link down (not shut down).") in _attention(case, device)


@pytest.mark.parametrize("state,words", [
    ({"admin": "admin-down", "oper": "admin-down"}, "shut down by configuration"),
    ({"admin": "up", "oper": "down"}, "up, line protocol down"),
    ({"admin": "unknown", "oper": "unknown", "not_read": True}, "not read"),
])
def test_port_words(state, words):
    case = _with_ports({(NEWY, NEWY_IF): {"admin": "up", "oper": "up"}, (RUTG, RUTG_IF): state})
    assert f"{RUTG} {RUTG_IF} {words}." in _attention(case, RUTG)


def test_the_parent_port_stands_in_for_an_unread_sub_interface():
    case = _with_ports({(NEWY, "HundredGigE0/0/0/19"): {"admin": "up", "oper": "up"}})
    assert f"{NEWY} {NEWY_IF} up; {RUTG} {RUTG_IF} not read." in _attention(case, NEWY)


def test_the_run_comparison_shows_why_it_is_down():
    def scan(state: dict) -> dict:
        return {"device_names": [NEWY, RUTG],
                "evidence": [{"kind": "spine", "role": "isis", "payload": {"operational_edges": [
                    {"id": EDGE, "state": state}]}}]}

    [row] = _routing_sessions_lost(scan({"local": "up", "remote": "up", "status": "up"}),
                                   scan({"local": "unknown", "remote": "unknown", "status": "down",
                                         "detail": "not reported by either router; both answered"}))
    assert _routing_session_line(row) == (f"IS-IS `{NEWY}` {NEWY_IF} ↔ `{RUTG}` {RUTG_IF} — "
                                          "down (not reported by either router; both answered)")


@pytest.mark.asyncio
async def test_the_coordinator_hands_the_expectation_to_the_isis_spine(monkeypatch):
    from diagnostic_mas import coordinator

    seen = {}

    async def isis_spine(*args, **kwargs):
        seen.update(kwargs)
        return {}

    async def nothing(*args, **kwargs):
        return {}

    monkeypatch.setattr(coordinator, "run_isis_spine", isis_spine)
    monkeypatch.setattr(coordinator, "run_bgp_spine", nothing)
    monkeypatch.setattr(coordinator, "run_service_spine", nothing)
    await coordinator.run_mandatory_spines(None, None, CaseFile(budget=Budget(0, 0)), [NEWY, RUTG],
                                           physical_edges=[], max_port_counter_reads=0,
                                           expected_isis_edges=[WAS_UP])
    assert seen["expected_edges"] == [WAS_UP]


# One router lists the adjacency (newy, as Init: it still hears rutg), the other answered without it.
NEWY_INIT = AdjacencyObservation(NEWY, "Hu0/0/0/19.3000", RUTG, "Init")
HALF = {("check_isis_adjacencies", NEWY): _adjacencies(NEWY, (RUTG, NEWY_IF)),
        ("check_isis_adjacencies", RUTG): _adjacencies(RUTG)}


def test_a_listing_the_other_router_does_not_share_is_kept_with_each_view():
    edges, issues = pair_adjacency_observations([NEWY_INIT], [NEWY, RUTG])
    assert edges == [{"id": isis_edge_id(NEWY, NEWY_IF, RUTG, "?"), "type": "isis_adjacency",
                      "local": {"device": NEWY, "interface": NEWY_IF}, "remote": {"device": RUTG, "interface": "?"},
                      "state": {"local": "init", "remote": "not listed", "status": "down"}}]
    assert [i["message"] for i in issues] == [
        f"IS-IS adjacency down: {NEWY} {NEWY_IF} lists it as Init; {RUTG} does not list it (its interface is not known)"]
    assert set(issues[0]["devices"]) == {NEWY, RUTG}

    # The far interface comes from a known adjacency, such as the previous scan's.
    edges, issues = pair_adjacency_observations([NEWY_INIT], [NEWY, RUTG], known_edges=[WAS_UP])
    assert edges[0]["id"] == EDGE
    assert issues[0]["message"] == f"IS-IS adjacency down: {NEWY} {NEWY_IF} lists it as Init; {RUTG} {RUTG_IF} does not list it"

    # A far router that was not read is still only "not read", and no fault.
    edges, issues = pair_adjacency_observations([NEWY_INIT], [NEWY, RUTG], unread_devices={RUTG})
    assert edges[0]["state"]["remote"] == "unknown" and issues == []


@pytest.mark.asyncio
async def test_an_expected_adjacency_one_router_still_lists_is_reported_once_with_each_view(monkeypatch):
    spine = await _spine(monkeypatch, {**CONFIGS, **HALF}, [WAS_UP])

    assert [e["state"] for e in spine["operational_edges"] if e["id"] == EDGE] == [
        {"local": "up", "remote": "not listed", "status": "unidirectional"}]
    assert _down(spine) == []                                   # not "both routers answered without it"
    assert [i["message"] for i in spine["issues"] if i["code"] == "unidirectional_adjacency"] == [
        f"IS-IS adjacency down: {NEWY} {NEWY_IF} lists it as Up; {RUTG} {RUTG_IF} does not list it"]


def _seen_differently() -> CaseFile:
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = [NEWY, RUTG]
    add_evidence(case, {"kind": "spine", "role": "isis", "payload": {
        "static_edges": [{"id": EDGE, "type": "isis_adjacency", "local": WAS_UP["local"], "remote": WAS_UP["remote"]}],
        "operational_edges": [{"id": EDGE, "state": {"local": "init", "remote": "not listed", "status": "down"}}]}})
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services", "payload": {"extra": {
        "physical_operational_edges": [{"id": f"if:{NEWY}:{NEWY_IF}", "state": {"admin": "up", "oper": "up"}},
                                       {"id": f"if:{RUTG}:{RUTG_IF}", "state": {"admin": "down", "oper": "down"}}]}}})
    open_issue(case, code="unidirectional_adjacency", severity="high", layer="underlay", evidence_ids=[],
               edge_id=EDGE, devices=[NEWY, RUTG],
               message=f"IS-IS adjacency down: {NEWY} {NEWY_IF} lists it as Init; {RUTG} {RUTG_IF} does not list it")
    return case


def test_an_adjacency_seen_differently_is_down_everywhere_and_never_called_unidirectional():
    case = _seen_differently()
    report = "\n".join([format_result_line(case), *format_devices_operator(case), *format_followup_operator(case)])

    assert f"1 IS-IS adjacency down: `{NEWY}` ↔ `{RUTG}`." in format_result_line(case)
    assert "Investigate 1 IS-IS adjacency down" in report
    for device, peer in ((NEWY, RUTG), (RUTG, NEWY)):
        block = _attention(case, device)
        assert f"lists it as Init; {RUTG} {RUTG_IF} does not list it. Ports as read this scan:" in block
        assert f"IS-IS 0/1 up; 1 down ({peer})" in block
    assert "unidirectional" not in report.lower()


def test_the_comparison_lists_an_adjacency_now_listed_by_one_router():
    def scan(state: dict) -> dict:
        return {"device_names": [NEWY, RUTG],
                "evidence": [{"kind": "spine", "role": "isis", "payload": {"operational_edges": [
                    {"id": EDGE, "state": state}]}}]}

    [row] = _routing_sessions_lost(scan({"local": "up", "remote": "up", "status": "up"}),
                                   scan({"local": "up", "remote": "not listed", "status": "unidirectional"}))
    assert _routing_session_line(row) == f"IS-IS `{NEWY}` {NEWY_IF} ↔ `{RUTG}` {RUTG_IF} — down (up / not listed)"


def test_a_neighbor_named_in_another_form_still_pairs_and_is_not_called_down():
    rutg = AdjacencyObservation(RUTG, "Hu0/0/0/23.3000", "NEWYDATASW", "Up")     # its system id, not the NSO name
    newy = AdjacencyObservation(NEWY, "Hu0/0/0/19.3000", RUTG, "Up")
    edges, issues = pair_adjacency_observations([newy, rutg], [NEWY, RUTG])
    assert [(e["id"], e["state"]["status"]) for e in edges] == [(EDGE, "up")] and issues == []
