"""Optics at both ends of an IS-IS adjacency that is down although both routers answered."""

from __future__ import annotations

import pytest

from diagnostic_mas.case import Budget, CaseFile, open_issue
from diagnostic_mas.link_optics import KIND, optics_line, parse_optics, record_link_optics
from diagnostic_mas.operator_report import format_devices_operator, format_run_details
from nso_facts.topology.graph import isis_edge_id

NEWY, RUTG = "newy-data-sw", "rutg-data-sw"
EDGE = isis_edge_id(NEWY, "HundredGigE0/0/0/19.3000", RUTG, "HundredGigE0/0/0/23.3000")


def _controllers(stamp: str, lanes: list[tuple[float, float]], alarms: str = "No alarms") -> str:
    """``show controllers <port>`` as IOS-XR prints it, trimmed to the parts read (22 Oct 2026 readings)."""
    rows = "\n".join(f"          {n}     n/a     {tx}   1.0000     {rx}   0.1000     55.000"
                     for n, (tx, rx) in enumerate(lanes))
    return f"""
{stamp}
Operational data for interface HundredGigE0/0/0/19:
State:
    Administrative state: enabled
    Operational state: Up
Phy:
    Digital Optical Monitoring:
        Transceiver Temp: 22.387 C
           Wavelength    Tx Power          Rx Power      Laser Bias
        Lane  (nm)    (dBm)    (mW)     (dBm)     (mW)      (mA)
        --   -----   ------   ------    ------   ------    ------
{rows}
        DOM alarms:
            {alarms}
        Alarm                     Alarm    Warning   Warning    Alarm
        Thresholds                High      High       Low       Low
        Receive Power (dBm):       5.500     4.500   -11.599   -12.604
Priority Flow Control:
    CoS  Status  Rx PFC Frames   Tx PFC Frames   Rx Dropped Data Frames
      0  off                  0               0                       0
"""


NEWY_TEXT = _controllers("Thu Oct 22 19:03:23.557 UTC", [(1.7, -2.5), (1.4, -0.4), (1.4, -1.8), (1.1, -1.2)])
RUTG_TEXT = _controllers("Thu Oct 22 19:03:23.969 UTC", [(1.1, -8.3), (1.7, -8.1), (1.9, -8.4), (1.7, -8.2)])
OUTPUTS = {(NEWY, "controllers HundredGigE0/0/0/19"): NEWY_TEXT, (RUTG, "controllers HundredGigE0/0/0/23"): RUTG_TEXT}


def _call(outputs: dict, asked: list):
    async def call(client, tool, args):
        assert tool == "exec_show"
        key = (args["device_name"], args["input_command"])
        asked.append(key)
        return {"status": "success", "data": {"result": outputs.get(key, "% Invalid input detected")}}
    return call


def _case(*edges: str, code: str = "unidirectional_adjacency") -> CaseFile:
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = sorted({part for edge in edges or (EDGE,) for part in edge.split(":")[1::2]})
    for edge in edges or (EDGE,):
        parts = edge.split(":")
        open_issue(case, code=code, severity="high", layer="underlay", evidence_ids=[], edge_id=edge,
                   devices=[parts[1], parts[3]], message=f"IS-IS adjacency down: {parts[1]} … {parts[3]} …")
    return case


def test_the_lane_table_and_alarms_are_read_and_nothing_else():
    reading = parse_optics(NEWY_TEXT)
    assert reading["lanes"] == [{"lane": 0, "tx_dbm": 1.7, "rx_dbm": -2.5}, {"lane": 1, "tx_dbm": 1.4, "rx_dbm": -0.4},
                                {"lane": 2, "tx_dbm": 1.4, "rx_dbm": -1.8}, {"lane": 3, "tx_dbm": 1.1, "rx_dbm": -1.2}]
    assert reading["alarms"] == "No alarms" and reading["read_at"] == "Thu Oct 22 19:03:23.557 UTC"
    assert parse_optics("% Invalid input detected") is None and parse_optics("") is None


@pytest.mark.asyncio
async def test_both_ends_are_read_and_each_direction_is_set_against_the_other():
    case, asked = _case(), []
    await record_link_optics(None, case, call=_call(OUTPUTS, asked))

    assert sorted(asked) == sorted(OUTPUTS)                        # the parent ports, once each
    assert optics_line(case, EDGE) == (
        f"Optics: {NEWY} → {RUTG}: transmitted +1.1 to +1.7 dBm, received -8.4 to -8.1 dBm, loss 9.3 to 10.0 dB "
        f"per lane; {RUTG} → {NEWY}: transmitted +1.1 to +1.9 dBm, received -2.5 to -0.4 dBm, loss 2.1 to 3.7 dB "
        "per lane. No DOM alarms.")
    text = "\n".join(format_devices_operator(case))
    for device in (NEWY, RUTG):
        assert "per lane. No DOM alarms." in text.split(f"### {device}\n", 1)[1].split("\n### ", 1)[0]
    assert "**Optics reads:** 2 of 2 ports" in "\n".join(format_run_details(case))


@pytest.mark.asyncio
async def test_an_end_not_read_is_said_and_alarms_are_named():
    outputs = {(NEWY, "controllers HundredGigE0/0/0/19"):
               _controllers("Thu Oct 22 19:03:23.557 UTC", [(1.7, -40.0)], alarms="Rx LOS, Rx Power Low Alarm")}
    case = _case()
    await record_link_optics(None, case, call=_call(outputs, []))

    assert optics_line(case, EDGE) == (
        f"Optics: {NEWY} → {RUTG}: transmitted +1.7 dBm, not read on {RUTG}; {RUTG} → {NEWY}: not read on {RUTG}, "
        f"received -40.0 dBm. DOM alarms on {NEWY}: Rx LOS, Rx Power Low Alarm.")


@pytest.mark.asyncio
async def test_reads_stop_at_the_ceiling_and_the_run_details_say_so():
    edges = [isis_edge_id(f"a{n}-data-sw", "HundredGigE0/0/0/1", f"b{n}-data-sw", "HundredGigE0/0/0/2") for n in range(3)]
    case, asked = _case(*edges), []
    await record_link_optics(None, case, ceiling=4, call=_call({}, asked))

    assert len(asked) == 4
    assert "**Optics reads:** 0 of 6 ports at the ends of IS-IS adjacencies down; 2 left out by the 4-read limit" \
        in "\n".join(format_run_details(case))
    assert optics_line(case, edges[2]) == "Optics not read."


@pytest.mark.asyncio
async def test_nothing_is_read_without_an_adjacency_down_or_with_an_unknown_far_interface():
    asked, quiet = [], CaseFile(budget=Budget(0, 0))
    await record_link_optics(None, quiet, call=_call(OUTPUTS, asked))
    half = isis_edge_id(NEWY, "HundredGigE0/0/0/19.3000", RUTG, "?")
    await record_link_optics(None, _case(half), call=_call(OUTPUTS, asked))

    assert asked == [(NEWY, "controllers HundredGigE0/0/0/19")]    # only the end whose interface is known
    assert not any(ev.get("kind") == KIND for ev in quiet.evidence)   # nothing down: nothing stored
