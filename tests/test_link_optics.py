"""Optics at both ends of an IS-IS adjacency that is down although both routers answered."""

from __future__ import annotations

import pytest

from diagnostic_mas.case import Budget, CaseFile, open_issue
from diagnostic_mas.link_optics import KIND, gaining_error_ends, optics_line, parse_optics, record_link_optics
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


ATLA, WASH = "atla-data-sw", "wash-data-sw"
BUNDLE_EDGE = isis_edge_id(ATLA, "Bundle-Ether100.3000", WASH, "Bundle-Ether101.3000")


def _bundle(name: str, members: list[str], active: int = 0) -> str:
    """``show bundle <name>`` as IOS-XR printed it on 3 Nov 2026 (atla↔wash), trimmed; MAC neutral."""
    rows = "\r\n".join(f"  {port:<20}  Local            Configured   0x8000, 0x000{6 - n}   400000000\r\n"
                       "      Link is down" for n, port in enumerate(members))
    return (f"\r\n\rTue Nov  3 07:44:09.578 UTC\r\n\r\n{name}\r\n"
            "  Status:                                    Down\r\n"
            f"  Local links <active/standby/configured>:   {active} / 0 / {len(members)}\r\n"
            "  Local bandwidth <effective/available>:     0 (0) kbps\r\n"
            "  MAC address (source):                      0000.0000.0001 (Chassis pool)\r\n"
            "  LACP:                                      Operational\r\n\r\n"
            "  Port                  Device           State        Port ID         B/W, kbps\r\n"
            "  --------------------  ---------------  -----------  --------------  ----------\r\n"
            f"{rows}\r\nRP/0/RP0/CPU0:router#")


def test_a_bundles_members_are_read_from_show_bundle():
    from diagnostic_mas.link_optics import parse_bundle

    reading = parse_bundle(_bundle("Bundle-Ether100", ["FH0/0/0/27", "FH0/0/0/28", "FH0/0/0/29"]))

    assert reading == {"active": 0, "configured": 3, "members": [
        {"port": f"FourHundredGigE0/0/0/{n}", "state": "Configured", "note": "Link is down"} for n in (27, 28, 29)]}
    assert parse_bundle("% Invalid input detected") is None


BUNDLE_OUTPUTS = {
    (ATLA, "bundle Bundle-Ether100"): _bundle("Bundle-Ether100", ["FH0/0/0/27", "FH0/0/0/28", "FH0/0/0/29"]),
    (WASH, "bundle Bundle-Ether101"): _bundle("Bundle-Ether101", ["FH0/0/0/40", "FH0/0/0/48", "FH0/0/0/52"]),
}
MEMBERS = (f"{ATLA} Bundle-Ether100, 0 of 3 members active: "
           + ", ".join(f"FourHundredGigE0/0/0/{n} (Configured, Link is down)" for n in (27, 28, 29)) + "; "
           f"{WASH} Bundle-Ether101, 0 of 3 members active: "
           + ", ".join(f"FourHundredGigE0/0/0/{n} (Configured, Link is down)" for n in (40, 48, 52)) + ".")


@pytest.mark.asyncio
async def test_a_bundle_end_lists_its_members_and_their_state_for_the_report_and_the_drill():
    from diagnostic_mas.drill import prior_exploration_for_issue

    case, asked = _case(BUNDLE_EDGE), []
    await record_link_optics(None, case, call=_call(BUNDLE_OUTPUTS, asked))

    # A bundle has no optics of its own; its members' optics are left to the investigation.
    assert asked == [(ATLA, "bundle Bundle-Ether100"), (WASH, "bundle Bundle-Ether101")]
    line = optics_line(case, BUNDLE_EDGE)
    assert line == f"Bundle members, each end on its own: {MEMBERS} Member optics not read."
    assert line in "\n".join(format_devices_operator(case))
    assert line in prior_exploration_for_issue(case, case.issues[0])["link_facts"]["readings"]
    assert "**Optics reads:** 0 of 0 ports at the ends of IS-IS adjacencies down; 2 bundles read for their members" \
        in "\n".join(format_run_details(case))


@pytest.mark.asyncio
async def test_bundle_reads_share_the_ceiling():
    case, asked = _case(BUNDLE_EDGE), []
    await record_link_optics(None, case, ceiling=1, call=_call(BUNDLE_OUTPUTS, asked))

    assert asked == [(ATLA, "bundle Bundle-Ether100")]
    assert optics_line(case, BUNDLE_EDGE).endswith(f"; {WASH} Bundle-Ether101: members not read. Member optics not read.")


@pytest.mark.asyncio
async def test_a_bundle_that_cannot_be_read_says_so():
    case = _case(BUNDLE_EDGE)
    await record_link_optics(None, case, call=_call({}, []))

    assert optics_line(case, BUNDLE_EDGE) == (
        f"Bundle members, each end on its own: {ATLA} Bundle-Ether100: members not read; "
        f"{WASH} Bundle-Ether101: members not read. Member optics not read.")


# --- links that are up but gaining errors: the light at both ends, so "CRC rising" comes with a reading ---------

def _port(**changed) -> dict:
    return {"state": "up", "transitions": 26, "cleared": "never", "description": "", "input_errors": 0, "crc": 0,
            "output_errors": 0, "input_drops": 0, "output_drops": 0, "services": 0, "adjacency": True,
            "device_services": 21} | changed


def _gaining_case(*, crc: int = 843) -> CaseFile:
    """An IS-IS adjacency that is up; the rutg end gained ``crc`` errors since the previous scan."""
    case = CaseFile(budget=Budget(0, 0))
    key = lambda device, port: f"{device}:{port}"
    case.previous_port_counters = {"read_at": "2026-10-22T16:00:00+00:00", "ports": {
        key(NEWY, "HundredGigE0/0/0/19"): _port(), key(RUTG, "HundredGigE0/0/0/23"): _port()}}
    case.evidence = [
        {"id": "ev_1", "kind": "spine", "role": "isis", "payload": {"operational_edges": [{"id": EDGE}]}},
        {"id": "ev_2", "kind": "port_counters", "role": "physical", "payload": {
            "read_at": "2026-10-22T19:00:00+00:00", "ports": {
                key(NEWY, "HundredGigE0/0/0/19"): _port(), key(RUTG, "HundredGigE0/0/0/23"): _port(crc=crc)}}}]
    return case


def _scans(case: CaseFile) -> tuple[dict, dict]:
    older = {"evidence": [{"kind": "port_counters", "payload": case.previous_port_counters}]}
    return older, {"evidence": case.evidence}


@pytest.mark.asyncio
async def test_a_link_gaining_errors_has_both_ends_read_even_though_it_is_up():
    case, asked = _gaining_case(), []

    await record_link_optics(None, case, call=_call(OUTPUTS, asked))

    assert sorted(asked) == sorted(OUTPUTS)                       # the gaining end and its far end
    saved = [ev["payload"] for ev in case.evidence if ev["kind"] == KIND][0]
    assert sorted(saved["ports"]) == [f"{NEWY}:HundredGigE0/0/0/19", f"{RUTG}:HundredGigE0/0/0/23"]
    assert saved["also_gaining"] == 2 and saved["wanted"] == 2


@pytest.mark.asyncio
async def test_no_port_is_read_as_gaining_errors_when_none_did_and_an_adjacency_down_keeps_its_place():
    quiet, asked = _gaining_case(crc=0), []
    await record_link_optics(None, quiet, call=_call(OUTPUTS, asked))
    saved = [ev["payload"] for ev in quiet.evidence if ev["kind"] == KIND][0]
    # The two ports are up, so the sweep reads them, but not as ends of a link that gained errors.
    assert sorted(asked) == sorted(OUTPUTS) and (saved["also_gaining"], saved["wanted"]) == (0, 0)
    assert len(saved["swept"]) == 2

    both, asked = _gaining_case(), []
    open_issue(both, code="unidirectional_adjacency", severity="high", layer="underlay", evidence_ids=[],
               edge_id=EDGE, devices=[NEWY, RUTG], message="IS-IS adjacency down")
    await record_link_optics(None, both, call=_call(OUTPUTS, asked))
    assert sorted(asked) == sorted(OUTPUTS)                       # the same two ports are asked for once, not twice
    assert [ev for ev in both.evidence if ev["kind"] == KIND][0]["payload"]["also_gaining"] == 0


@pytest.mark.asyncio
async def test_the_report_line_of_a_link_gaining_errors_carries_the_light_at_each_end():
    from diagnostic_mas.link_optics import with_optics
    from diagnostic_mas.port_counters import gain_lines, links_gaining_errors

    case = _gaining_case()
    await record_link_optics(None, case, call=_call(OUTPUTS, []))
    older, newer = _scans(case)

    rows = with_optics(links_gaining_errors(older, newer), newer)

    assert rows[0]["optics"] == (
        f"Optics, each end on its own: {RUTG} transmitted +1.1 to +1.9 dBm, received -8.4 to -8.1 dBm; "
        f"{NEWY} transmitted +1.1 to +1.7 dBm, received -2.5 to -0.4 dBm. No DOM alarms.")
    assert gain_lines(rows)[0].endswith(" " + rows[0]["optics"])


def test_a_link_gaining_errors_says_nothing_about_light_when_none_was_read():
    from diagnostic_mas.link_optics import with_optics
    from diagnostic_mas.port_counters import gain_lines, links_gaining_errors

    older, newer = _scans(_gaining_case())
    rows = with_optics(links_gaining_errors(older, newer), newer)

    assert "optics" not in rows[0] and "Optics" not in gain_lines(rows)[0]


@pytest.mark.asyncio
async def test_an_alarm_on_either_end_is_named_and_an_end_not_read_is_said():
    from diagnostic_mas.link_optics import with_optics
    from diagnostic_mas.port_counters import links_gaining_errors

    outputs = {(RUTG, "controllers HundredGigE0/0/0/23"):
               _controllers("Thu Oct 22 19:03:23.969 UTC", [(1.1, -9.8)], alarms="Rx Power Low Alarm")}
    case = _gaining_case()
    await record_link_optics(None, case, call=_call(outputs, []))
    older, newer = _scans(case)

    text = with_optics(links_gaining_errors(older, newer), newer)[0]["optics"]

    assert text == (f"Optics, each end on its own: {RUTG} transmitted +1.1 dBm, received -9.8 dBm; {NEWY} not read. "
                    f"DOM alarms on {RUTG}: Rx Power Low Alarm.")


def test_the_run_details_say_how_many_of_the_reads_were_for_links_gaining_errors():
    case = _gaining_case()
    case.evidence.append({"id": "ev_3", "kind": KIND, "payload": {
        "ports": {f"{RUTG}:HundredGigE0/0/0/23": {"lanes": []}}, "wanted": 4, "left_out": 0, "ceiling": 20, "also_gaining": 2}})

    assert ("**Optics reads:** 1 of 4 ports at the ends of IS-IS adjacencies down and of links gaining errors"
            in "\n".join(format_run_details(case)))


def test_a_breakout_lane_is_chosen_together_with_its_far_end():
    from diagnostic_mas.link_optics import gaining_error_ends

    toky, seat = "toky-data-sw", "seat-data-sw"
    edge = isis_edge_id(toky, "TenGigE0/0/0/18/0.1491", seat, "HundredGigE0/0/0/19.1491")
    case = CaseFile(budget=Budget(0, 0))
    lane = f"{toky}:TenGigE0/0/0/18/0"
    case.previous_port_counters = {"read_at": "2026-10-22T16:00:00+00:00", "ports": {lane: _port()}}
    case.evidence = [
        {"id": "ev_1", "kind": "spine", "role": "isis", "payload": {"operational_edges": [{"id": edge}]}},
        {"id": "ev_2", "kind": "port_counters", "role": "physical", "payload": {
            "read_at": "2026-10-22T19:00:00+00:00", "ports": {lane: _port(crc=900)}}}]

    assert gaining_error_ends(case) == [(toky, "TenGigE0/0/0/18/0"), (seat, "HundredGigE0/0/0/19")]



# --- a breakout lane's light, from its parent port: `controllers optics 0/0/0/64` as star-data-sw answered on 12 Nov ---
# (a 400G DR4 port in four lanes: lanes 1 and 2 dark, 0 and 3 up; the vendor details at the end are left out)

STAR_PARENT = """

Thu Nov 12 21:34:56.522 UTC

 Controller State: Up 

 Transport Admin State: In Service 

 Laser State: On 
 
 Host Squelch Status: Enable 

 LED State: Green 

 FEC State: FEC DISABLED 
 
 Optics Status 

	 Optics Type:  QSFPDD 400G DR4
	 Wavelength = 1311.00 nm 
	 Loopback Host : None 
	 Loopback Media : None 

	 Alarm Status:
	 -------------
	 Detected Alarms: 
		 LOW-RX2-PWR   LOW-RX3-PWR   

	 LOS/LOL/Fault Status:
	 Detected LOS/LOL/FAULT: RX-LOS   



	 Performance Monitoring: Disable	

	 THRESHOLD VALUES
	 ----------------

	 Parameter                 High Alarm  Low Alarm  High Warning  Low Warning
	 ------------------------  ----------  ---------  ------------  -----------
	 Rx Power Threshold(dBm)          4.9       -8.9           3.9         -5.9
	 Tx Power Threshold(dBm)          4.9       -5.9           3.9         -2.9
	 LBC Threshold(mA)             440.00     100.00        400.00       120.00
	 Temp. Threshold(celsius)       75.00      -5.00         70.00         0.00
	 Voltage Threshold(volt)         3.63       2.97          3.46         3.13

	 Configured Tx Power = 0.00 dBm 
	 Configured CD High Threshold = 0 ps/nm 
	 Configured CD lower Threshold = 0 ps/nm 
	 Configured OSNR lower Threshold = 0.00 dB 
	 Configured DGD Higher Threshold = 0.00 ps 
	 Polarization parameters not supported by optics

	 Lane  Laser Bias    TX Power    RX Power  Output Frequency
	 ----  ----------  ----------  ----------  ----------------
	   0     74.8 mA    2.44 dBm    1.64 dBm       N/A  
	   1     74.8 mA    2.29 dBm  -40.00 dBm       N/A  
	   2     74.8 mA    2.16 dBm  -40.00 dBm       N/A  
	   3     74.8 mA    2.24 dBm    1.10 dBm       N/A  

	 Temperature = 39.81 Celsius 
	 Voltage = 3.29 V


RP/0/RP0/CPU0:star-data-sw#"""


def test_the_parent_ports_lane_table_thresholds_and_alarms_are_read():
    from diagnostic_mas.link_optics import parse_parent_optics

    reading = parse_parent_optics(STAR_PARENT)

    assert reading["lanes"] == [{"lane": 0, "tx_dbm": 2.44, "rx_dbm": 1.64}, {"lane": 1, "tx_dbm": 2.29, "rx_dbm": -40.0},
                                {"lane": 2, "tx_dbm": 2.16, "rx_dbm": -40.0}, {"lane": 3, "tx_dbm": 2.24, "rx_dbm": 1.10}]
    assert reading["alarms"] == "LOW-RX2-PWR LOW-RX3-PWR"
    assert (reading["rx_low_alarm_dbm"], reading["rx_low_warning_dbm"]) == (-8.9, -5.9)
    assert reading["read_at"] == "Thu Nov 12 21:34:56.522 UTC"
    assert parse_parent_optics("% Invalid input detected") is None and parse_parent_optics("") is None


def _lane_case(port: str = "HundredGigE0/0/0/64/3", far: str = "HundredGigE0/0/0/19.1491") -> CaseFile:
    star, seat = "star-data-sw", "seat-data-sw"
    edge = isis_edge_id(star, port + ".1491", seat, far)
    case = CaseFile(budget=Budget(0, 0))
    key = f"{star}:{port}"
    case.previous_port_counters = {"read_at": "2026-10-22T16:00:00+00:00", "ports": {key: _port()}}
    case.evidence = [
        {"id": "ev_1", "kind": "spine", "role": "isis", "payload": {"operational_edges": [{"id": edge}]}},
        {"id": "ev_2", "kind": "port_counters", "role": "physical", "payload": {
            "read_at": "2026-10-22T19:00:00+00:00", "ports": {key: _port(crc=900)}}}]
    return case


@pytest.mark.asyncio
async def test_breakout_lanes_share_one_read_of_their_parent_port_and_each_keeps_its_own_lane():
    case, asked = _lane_case(), []
    # a second lane of the same port gained errors too
    case.previous_port_counters["ports"]["star-data-sw:HundredGigE0/0/0/64/0"] = _port()
    case.evidence[1]["payload"]["ports"]["star-data-sw:HundredGigE0/0/0/64/0"] = _port(crc=7)
    outputs = {("star-data-sw", "controllers optics 0/0/0/64"): STAR_PARENT}

    await record_link_optics(None, case, call=_call(outputs, asked))

    assert asked.count(("star-data-sw", "controllers optics 0/0/0/64")) == 1            # one read for two lanes
    saved = [ev["payload"] for ev in case.evidence if ev["kind"] == KIND][0]["ports"]
    lane3, lane0 = saved["star-data-sw:HundredGigE0/0/0/64/3"], saved["star-data-sw:HundredGigE0/0/0/64/0"]
    assert lane3["lanes"] == [{"lane": 3, "tx_dbm": 2.24, "rx_dbm": 1.10}] and lane3["parent"] == "0/0/0/64"
    assert lane0["lanes"] == [{"lane": 0, "tx_dbm": 2.44, "rx_dbm": 1.64}]
    assert lane3["alarms"] == "" and lane0["alarms"] == ""                                # LOW-RX2/3 belong to lanes 1 and 2


@pytest.mark.asyncio
async def test_a_dark_lane_shows_its_light_its_alarm_and_the_thresholds_on_the_report_line():
    from diagnostic_mas.link_optics import with_optics
    from diagnostic_mas.port_counters import links_gaining_errors

    case = _lane_case(port="HundredGigE0/0/0/64/2")
    outputs = {("star-data-sw", "controllers optics 0/0/0/64"): STAR_PARENT}
    await record_link_optics(None, case, call=_call(outputs, []))
    older = {"evidence": [{"kind": "port_counters", "payload": case.previous_port_counters}]}
    newer = {"evidence": case.evidence}

    text = with_optics(links_gaining_errors(older, newer), newer)[0]["optics"]

    assert text == ("Optics, each end on its own: star-data-sw HundredGigE0/0/0/64/2 (lane 2 of 0/0/0/64) transmitted "
                    "+2.2 dBm, received -40.0 dBm (receive low alarm -8.9 dBm); seat-data-sw not read. "
                    "DOM alarms on star-data-sw: LOW-RX3-PWR.")


@pytest.mark.asyncio
async def test_a_parent_read_that_cannot_be_parsed_leaves_the_lane_not_read():
    case = _lane_case()
    await record_link_optics(None, case, call=_call({("star-data-sw", "controllers optics 0/0/0/64"): "% Invalid input"}, []))

    saved = [ev["payload"] for ev in case.evidence if ev["kind"] == KIND][0]
    assert saved["ports"] == {} and saved["wanted"] == 2                 # the lane and its far end, neither readable


# --- a follow-up item when the device itself reports an optics alarm on a link that gained errors ------------------

async def _alarmed_rows(*, far_read: bool):
    from diagnostic_mas.link_optics import with_optics
    from diagnostic_mas.port_counters import links_gaining_errors

    case = _lane_case(port="HundredGigE0/0/0/64/2")
    outputs = {("star-data-sw", "controllers optics 0/0/0/64"): STAR_PARENT}
    if far_read:
        outputs[("seat-data-sw", "controllers HundredGigE0/0/0/19")] = NEWY_TEXT
    await record_link_optics(None, case, call=_call(outputs, []))
    older = {"evidence": [{"kind": "port_counters", "payload": case.previous_port_counters}]}
    newer = {"evidence": case.evidence}
    return with_optics(links_gaining_errors(older, newer), newer)


@pytest.mark.asyncio
async def test_a_row_keeps_the_alarm_the_device_reported_as_data_not_only_as_text():
    rows = await _alarmed_rows(far_read=True)

    [alarm] = rows[0]["optics_alarms"]
    assert (alarm["device"], alarm["port"], alarm["alarms"]) == ("star-data-sw", "HundredGigE0/0/0/64/2", "LOW-RX3-PWR")
    assert (alarm["rx_dbm"], alarm["rx_low_alarm_dbm"], alarm["parent"], alarm["lane"]) == (-40.0, -8.9, "0/0/0/64", 2)
    assert alarm["other"] == {"device": "seat-data-sw", "tx": "+1.1 to +1.7"}


@pytest.mark.asyncio
async def test_no_alarm_data_when_the_device_reports_none():
    from diagnostic_mas.link_optics import with_optics
    from diagnostic_mas.port_counters import links_gaining_errors

    case = _gaining_case()
    await record_link_optics(None, case, call=_call(OUTPUTS, []))
    older, newer = _scans(case)

    assert "optics_alarms" not in with_optics(links_gaining_errors(older, newer), newer)[0]


@pytest.mark.asyncio
async def test_the_follow_up_list_names_a_receive_level_below_the_devices_own_alarm_and_the_far_ends_transmit():
    from diagnostic_mas.operator_report import format_followup_operator

    rows = await _alarmed_rows(far_read=True)
    delta = {"links_gaining_errors": rows, "item_history": {}}

    items = format_followup_operator(CaseFile(budget=Budget(0, 0)), None, case_delta=delta)

    assert items == [
        "1. Investigate `star-data-sw` HundredGigE0/0/0/64/2 (lane 2 of 0/0/0/64): the device reports its receive level "
        "(-40.0 dBm) below its low alarm (-8.9 dBm, LOW-RX3-PWR) while `seat-data-sw` transmits +1.1 to +1.7 dBm; "
        "the port gained errors since the previous scan."]


@pytest.mark.asyncio
async def test_the_item_leaves_out_the_far_end_when_it_was_not_read_and_the_list_is_none_without_alarms():
    from diagnostic_mas.operator_report import format_followup_operator

    rows = await _alarmed_rows(far_read=False)
    items = format_followup_operator(CaseFile(budget=Budget(0, 0)), None, case_delta={"links_gaining_errors": rows})
    assert items[0].endswith("(-8.9 dBm, LOW-RX3-PWR); the port gained errors since the previous scan.")
    assert "while" not in items[0]

    quiet = [{k: v for k, v in rows[0].items() if k != "optics_alarms"}]
    assert format_followup_operator(CaseFile(budget=Budget(0, 0)), None, case_delta={"links_gaining_errors": quiet}) == ["(none)"]
    assert format_followup_operator(CaseFile(budget=Budget(0, 0)), None) == ["(none)"]


# --- a sweep of the ports on links that are up: a transceiver in alarm before anything else shows --------------------

A_SW = "a-sw"
LOW = "Rx Power Low Alarm"


def _sweep_case() -> CaseFile:
    """Counters were read on six ports; no adjacency is down and nothing gained errors."""
    def up(**changed):
        return _port(**changed)
    case = CaseFile(budget=Budget(0, 0))
    case.evidence = [{"id": "ev_1", "kind": "port_counters", "role": "physical", "payload": {
        "read_at": "2026-10-22T19:00:00+00:00", "ports": {
            f"{A_SW}:HundredGigE0/0/0/1": up(), f"{A_SW}:HundredGigE0/0/0/2": up(),
            f"{A_SW}:HundredGigE0/0/0/3": up(state="down"),                       # down: not a link that is up
            f"{A_SW}:Bundle-Ether100": up(),                                       # a bundle has no optics of its own
            "star-data-sw:HundredGigE0/0/0/64/0": up(), "star-data-sw:HundredGigE0/0/0/64/1": up()}}}]
    return case


SWEEP_OUTPUTS = {
    (A_SW, "controllers HundredGigE0/0/0/1"): _controllers("Thu Oct 22 19:03:23.557 UTC", [(1.7, -2.5)]),
    (A_SW, "controllers HundredGigE0/0/0/2"): _controllers("Thu Oct 22 19:03:24.000 UTC", [(1.1, -12.4)], alarms=LOW),
    ("star-data-sw", "controllers optics 0/0/0/64"): STAR_PARENT,
}


@pytest.mark.asyncio
async def test_the_sweep_reads_each_port_that_is_up_once_and_a_breakout_parent_once():
    case, asked = _sweep_case(), []

    await record_link_optics(None, case, call=_call(SWEEP_OUTPUTS, asked))

    assert sorted(asked) == sorted(SWEEP_OUTPUTS)                        # not the down port, not the bundle
    saved = [ev["payload"] for ev in case.evidence if ev["kind"] == KIND][0]
    assert saved["swept"] == [f"{A_SW}:HundredGigE0/0/0/1", f"{A_SW}:HundredGigE0/0/0/2",
                              "star-data-sw:HundredGigE0/0/0/64/0", "star-data-sw:HundredGigE0/0/0/64/1"]
    assert sorted(saved["ports"]) == saved["swept"]
    assert (saved["wanted"], saved["also_gaining"]) == (0, 0)


@pytest.mark.asyncio
async def test_a_port_already_read_for_a_link_gaining_errors_is_not_swept_again():
    case, asked = _gaining_case(), []
    await record_link_optics(None, case, call=_call(OUTPUTS, asked))

    saved = [ev["payload"] for ev in case.evidence if ev["kind"] == KIND][0]
    assert sorted(asked) == sorted(OUTPUTS) and saved["swept"] == []


@pytest.mark.asyncio
async def test_the_sweep_stops_at_its_own_limit_and_says_so(monkeypatch):
    import diagnostic_mas.link_optics as lo

    monkeypatch.setattr(lo, "SWEEP_CEILING", 1)
    case, asked = _sweep_case(), []
    await record_link_optics(None, case, call=_call(SWEEP_OUTPUTS, asked))

    saved = [ev["payload"] for ev in case.evidence if ev["kind"] == KIND][0]
    assert len(asked) == 1 and saved["sweep_left_out"] == 3


@pytest.mark.asyncio
async def test_only_swept_ports_with_an_alarm_of_their_own_are_leads():
    from diagnostic_mas.link_optics import optics_alarm_leads

    case = _sweep_case()
    await record_link_optics(None, case, call=_call(SWEEP_OUTPUTS, []))

    leads = optics_alarm_leads(case)

    assert [(lead["device"], lead["port"], lead["alarms"]) for lead in leads] == [
        (A_SW, "HundredGigE0/0/0/2", LOW), ("star-data-sw", "HundredGigE0/0/0/64/1", "LOW-RX2-PWR")]
    assert leads[1]["parent"] == "0/0/0/64" and leads[1]["lane"] == 1
    assert optics_alarm_leads(CaseFile(budget=Budget(0, 0))) == []


@pytest.mark.asyncio
async def test_the_run_details_say_how_many_swept_ports_were_read_and_how_many_have_an_alarm():
    case = _sweep_case()
    await record_link_optics(None, case, call=_call(SWEEP_OUTPUTS, []))

    assert ("sweep of ports on links that are up: 4 of 4 read, 2 with an alarm"
            in "\n".join(format_run_details(case)))


@pytest.mark.asyncio
async def test_the_follow_up_list_and_the_result_name_the_alarms_as_a_lead_not_a_status():
    from diagnostic_mas.operator_report import format_followup_operator, format_result_section

    case = _sweep_case()
    await record_link_optics(None, case, call=_call(SWEEP_OUTPUTS, []))

    assert format_followup_operator(case) == [
        "1. Investigate the optics alarms the devices themselves report on 2 links that are up: "
        f"`{A_SW}` HundredGigE0/0/0/2 ({LOW}), `star-data-sw` HundredGigE0/0/0/64/1 (lane 1 of 0/0/0/64, LOW-RX2-PWR). "
        "No service status rests on them."]
    result = "\n".join(format_result_section(case, None))
    assert "- **Optics alarms:** 2 ports on links that are up report an optics alarm of their own (a lead, not a status)." in result
    quiet = _sweep_case()
    await record_link_optics(None, quiet, call=_call({k: v for k, v in SWEEP_OUTPUTS.items() if "64" in k[1]
                                                      and False}, []))
    assert "Optics alarms" not in "\n".join(format_result_section(quiet, None))


@pytest.mark.asyncio
async def test_many_alarms_are_listed_five_and_then_counted():
    from diagnostic_mas.operator_report import format_followup_operator

    case = CaseFile(budget=Budget(0, 0))
    ports = {f"{A_SW}:HundredGigE0/0/0/{n}": _port() for n in range(1, 9)}
    case.evidence = [{"id": "ev_1", "kind": "port_counters", "role": "physical", "payload": {
        "read_at": "2026-10-22T19:00:00+00:00", "ports": ports}}]
    outputs = {(A_SW, f"controllers HundredGigE0/0/0/{n}"): _controllers("Thu Oct 22 19:03:24.000 UTC", [(1.1, -12.4)], alarms=LOW)
               for n in range(1, 9)}
    await record_link_optics(None, case, call=_call(outputs, []))

    [item] = format_followup_operator(case)
    assert "on 8 links that are up: " in item and item.count("HundredGigE0/0/0/") == 5 and "and 3 more" in item


@pytest.mark.asyncio
async def test_the_metrics_snapshot_carries_the_sweep_counts_only_when_the_sweep_ran():
    from diagnostic_mas.metrics import metrics_snapshot_from_case

    case = _sweep_case()
    assert "optics" not in metrics_snapshot_from_case(case)               # no sweep yet: no claim of zero alarms

    await record_link_optics(None, case, call=_call(SWEEP_OUTPUTS, []))

    assert metrics_snapshot_from_case(case)["optics"] == {"alarm_ports": 2, "ports_read": 4}


@pytest.mark.asyncio
async def test_a_port_that_only_gained_output_drops_is_not_read_for_light_but_one_that_also_gained_crc_is():
    drops = _gaining_case(crc=0)
    drops.evidence[1]["payload"]["ports"][f"{RUTG}:HundredGigE0/0/0/23"]["output_drops"] = 900
    assert gaining_error_ends(drops) == []                                   # congestion is not a light question

    both = _gaining_case()
    both.evidence[1]["payload"]["ports"][f"{RUTG}:HundredGigE0/0/0/23"]["output_drops"] = 900
    assert (RUTG, "HundredGigE0/0/0/23") in gaining_error_ends(both)


def test_a_lane_row_with_a_flag_after_its_bias_is_still_a_lane():
    """wash FourHundredGigE0/0/0/64 on 13 Nov: three of its four lanes carry a flag, and only the unflagged lane was read."""
    text = _controllers("Fri Nov 13 04:59:07.000 UTC", [(2.1, 4.1), (2.2, 3.4), (2.1, 1.6), (2.2, 3.3)],
                        alarms="Receive Power: Alarm high")
    text = (text.replace("4.1   0.1000     55.000", "4.1   0.1000     55.000   H - Rx alarm high")
                .replace("3.4   0.1000     55.000", "3.4   0.1000     55.000   h - Rx warning high")
                .replace("3.3   0.1000     55.000", "3.3   0.1000     55.000   h - Rx warning high"))

    parsed = parse_optics(text)

    assert [(lane["lane"], lane["rx_dbm"]) for lane in parsed["lanes"]] == [(0, 4.1), (1, 3.4), (2, 1.6), (3, 3.3)]
    assert parsed["alarms"] == "Receive Power: Alarm high"
