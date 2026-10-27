"""LLDP at both ends of an IS-IS adjacency that is down although both routers answered."""

from __future__ import annotations

import pytest

from diagnostic_mas.case import Budget, CaseFile, open_issue
from diagnostic_mas.drill import prior_exploration_for_issue
from diagnostic_mas.link_lldp import KIND, far_port, lldp_line, parse_lldp, record_link_lldp
from diagnostic_mas.link_optics import optics_line, record_link_optics
from diagnostic_mas.operator_report import format_devices_operator, format_run_details
from nso_facts.topology.graph import isis_edge_id

SALT, SEAT = "salt-data-sw", "seat-data-sw"
# salt does not list the adjacency, so its interface is not known (26 Oct 2026).
EDGE = isis_edge_id(SALT, "?", SEAT, "HundredGigE0/0/0/23.3000")


def _table(stamp: str, rows: list[str]) -> str:
    """``show lldp neighbors`` as IOS-XR prints it."""
    body = "\r\n".join(rows)
    return (f"\r\n\r{stamp}\r\nCapability codes:\r\n\t(R) Router, (B) Bridge, (T) Telephone, (C) DOCSIS Cable Device\r\n"
            "\t(W) WLAN Access Point, (P) Repeater, (S) Station, (O) Other\r\n\r\n"
            "Device ID       Local Intf                      Hold-time  Capability      Port ID\r\n"
            f"{body}\r\n\r\nTotal entries displayed: {len(rows)}\r\n\r\nRP/0/RP0/CPU0:router#")


SALT_TEXT = _table("Mon Oct 26 20:08:10.524 UTC", [
    "kans-data-sw.kans.fa HundredGigE0/0/0/17             120        R               HundredGigE0/0/0/23",
    "utah-data-sw.fabric. HundredGigE0/0/0/19             120        R               HundredGigE0/0/0/23",
    "localhost.localdomai FourHundredGigE0/0/0/28         120        S               0000.0000.0001",
    "losa-data-sw.fabric. FourHundredGigE0/0/0/44         120        R               Bundle-Ether100",
])
SEAT_TEXT = _table("Mon Oct 26 20:08:21.434 UTC", [
    "[DISABLED]      TwentyFiveGigE0/0/0/12/0        30         N/A             0000.0000.0002",
    "peer-1.example.net   HundredGigE0/0/0/19             120        B,R             644",
    "losa-data-sw.fabric. HundredGigE0/0/0/22             120        R               HundredGigE0/0/0/23",
    "salt-data-sw.fabric. HundredGigE0/0/0/23             120        R               HundredGigE0/0/0/21",
])
OUTPUTS = {(SALT, "lldp neighbors"): SALT_TEXT, (SEAT, "lldp neighbors"): SEAT_TEXT}


def _call(outputs: dict, asked: list):
    async def call(client, tool, args):
        assert tool == "exec_show"
        key = (args["device_name"], args["input_command"])
        asked.append(key)
        return {"status": "success", "data": {"result": outputs.get(key, "% Invalid input detected")}}
    return call


def _case(*edges: str) -> CaseFile:
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = sorted({part for edge in edges or (EDGE,) for part in edge.split(":")[1::2]})
    for edge in edges or (EDGE,):
        parts = edge.split(":")
        open_issue(case, code="unidirectional_adjacency", severity="high", layer="underlay", evidence_ids=[],
                   edge_id=edge, devices=[parts[1], parts[3]], message=f"IS-IS adjacency down: {parts[1]} … {parts[3]} …")
    return case


def test_each_row_is_read_with_the_neighbor_the_local_port_and_the_neighbor_port():
    rows = parse_lldp(SEAT_TEXT)

    assert rows[-1] == {"neighbor": "salt-data-sw.fabric.", "local": "HundredGigE0/0/0/23",
                        "port": "HundredGigE0/0/0/21"}
    assert [row["neighbor"] for row in rows] == ["[DISABLED]", "peer-1.example.net", "losa-data-sw.fabric.",
                                                 "salt-data-sw.fabric."]
    assert parse_lldp(_table("Mon Oct 26 20:08:10.524 UTC", [])) == []    # read, and no neighbors
    assert parse_lldp("% Invalid input detected") is None and parse_lldp("") is None


@pytest.mark.asyncio
async def test_each_direction_is_told_by_the_receiving_end_and_the_unknown_port_is_named():
    case, asked = _case(), []
    await record_link_lldp(None, case, call=_call(OUTPUTS, asked))

    assert sorted(asked) == sorted(OUTPUTS)                       # each end's table, once
    assert far_port(case, EDGE, SALT) == "HundredGigE0/0/0/21"     # from seat's table
    assert far_port(case, EDGE, SEAT) is None                      # already named by the adjacency
    assert lldp_line(case, EDGE) == (
        f"LLDP: {SALT} → {SEAT} heard ({SEAT} HundredGigE0/0/0/23 lists {SALT} HundredGigE0/0/0/21); "
        f"{SEAT} → {SALT} not heard ({SALT} HundredGigE0/0/0/21 lists no LLDP neighbor).")


@pytest.mark.asyncio
async def test_a_port_that_hears_another_router_says_which():
    salt = _table("Mon Oct 26 20:08:10.524 UTC", [
        "utah-data-sw.fabric. HundredGigE0/0/0/21             120        R               HundredGigE0/0/0/23"])
    case = _case()
    case.device_names.append("utah-data-sw")
    await record_link_lldp(None, case, call=_call({**OUTPUTS, (SALT, "lldp neighbors"): salt}, []))

    assert lldp_line(case, EDGE).endswith(
        f"{SEAT} → {SALT} not heard ({SALT} HundredGigE0/0/0/21 lists utah-data-sw HundredGigE0/0/0/23 instead).")


@pytest.mark.asyncio
async def test_an_end_not_read_is_not_read_and_a_port_not_named_stays_unknown():
    case = _case()
    await record_link_lldp(None, case, call=_call({(SALT, "lldp neighbors"): SALT_TEXT}, []))
    # seat's table was not read, so salt's port is not known either.
    assert far_port(case, EDGE, SALT) is None
    assert lldp_line(case, EDGE) == (
        f"LLDP: {SALT} → {SEAT} not read on {SEAT}; {SEAT} → {SALT}: {SALT}'s port not known.")

    unread = _case()
    await record_link_lldp(None, unread, call=_call({}, []))
    assert lldp_line(unread, EDGE) == "LLDP not read."


@pytest.mark.asyncio
async def test_the_optics_read_reaches_the_port_lldp_named():
    from test_link_optics import _controllers

    case, asked = _case(), []
    outputs = {**OUTPUTS,
               (SALT, "controllers HundredGigE0/0/0/21"):
               _controllers("Mon Oct 26 20:08:30.000 UTC", [(1.2, -40.0)], alarms="Rx LOS"),
               (SEAT, "controllers HundredGigE0/0/0/23"):
               _controllers("Mon Oct 26 20:08:31.000 UTC", [(1.6, -0.5)])}
    await record_link_lldp(None, case, call=_call(outputs, asked))
    await record_link_optics(None, case, call=_call(outputs, asked))

    assert (SALT, "controllers HundredGigE0/0/0/21") in asked
    assert optics_line(case, EDGE) == (
        f"Optics: {SALT} → {SEAT}: transmitted +1.2 dBm, received -0.5 dBm, loss 1.7 dB per lane; "
        f"{SEAT} → {SALT}: transmitted +1.6 dBm, received -40.0 dBm, loss 41.6 dB per lane. "
        f"DOM alarms on {SALT}: Rx LOS.")


@pytest.mark.asyncio
async def test_the_report_and_the_drill_get_the_directions():
    case = _case()
    await record_link_lldp(None, case, call=_call(OUTPUTS, []))

    text = "\n".join(format_devices_operator(case))
    salt = text.split(f"### {SALT}\n", 1)[1].split("\n### ", 1)[0]
    assert f"{SALT} HundredGigE0/0/0/21 (named by {SEAT}'s LLDP)" in salt
    assert f"{SEAT} → {SALT} not heard ({SALT} HundredGigE0/0/0/21 lists no LLDP neighbor)." in salt
    assert "**LLDP reads:** 2 of 2 routers at the ends of IS-IS adjacencies down" in "\n".join(format_run_details(case))

    facts = prior_exploration_for_issue(case, case.issues[0])["link_facts"]
    assert lldp_line(case, EDGE) in facts["readings"]


@pytest.mark.asyncio
async def test_nothing_is_read_without_an_adjacency_down_and_reads_stop_at_the_ceiling():
    asked, quiet = [], CaseFile(budget=Budget(0, 0))
    await record_link_lldp(None, quiet, call=_call(OUTPUTS, asked))
    assert asked == [] and not any(ev.get("kind") == KIND for ev in quiet.evidence)

    edges = [isis_edge_id(f"a{n}-data-sw", "HundredGigE0/0/0/1", f"b{n}-data-sw", "HundredGigE0/0/0/2") for n in range(3)]
    case = _case(*edges)
    await record_link_lldp(None, case, ceiling=4, call=_call({}, asked))
    assert len(asked) == 4
    assert "**LLDP reads:** 0 of 6 routers at the ends of IS-IS adjacencies down; 2 left out by the 4-read limit" \
        in "\n".join(format_run_details(case))
