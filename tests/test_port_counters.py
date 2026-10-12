"""Error counters on ports that carry a service or an adjacency, compared between scans.

The interface output is as rutg-data-sw returned it for its uplink on 8 October 2026.
A port that is up and gained errors is a lead; it never changes a service's status.
"""
import pytest

from diagnostic_mas.case import Budget, CaseFile, add_evidence
from diagnostic_mas.case_delta import compute_case_delta, format_changes_since_previous
from diagnostic_mas.open_faults import digest_changes
from diagnostic_mas.port_counters import (
    counter_targets, links_gaining_errors, parse_port_counters, record_port_counters)
from diagnostic_mas.state_paths import case_to_dict

RUTG, HUB = "rutg-data-sw", "star-data-sw"
UPLINK = "HundredGigE0/0/0/23"
SHOWN = """

Thu Oct  8 05:41:06.561 UTC
HundredGigE0/0/0/23 is up, line protocol is up
  Interface state transitions: 26
  Hardware is HundredGigE, address is bc2c.e6b3.d760 (bia bc2c.e6b3.d760)
  Description: uplink to star, panel 4
  Internet address is Unknown
  MTU 9216 bytes, BW 100000000 Kbit (Max: 100000000 Kbit)
     reliability 255/255, txload 0/255, rxload 0/255
  Encapsulation ARPA,
  Full-duplex, 100000Mb/s, 100GBASE-LR4, link type is force-up
  output flow control is off, input flow control is off
  Carrier delay (up) is 200 msec
  loopback not set,
  Last link flapped 01:33:26
  Last input 01:33:12, output 01:33:12
  Last clearing of "show interface" counters never
  5 minute input rate 0 bits/sec, 0 packets/sec
  5 minute output rate 0 bits/sec, 0 packets/sec
     569938104308 packets input, 1315276986914302 bytes, 0 total input drops
     0 drops for unrecognized upper-level protocol
     Received 16 broadcast packets, 11201155 multicast packets
              0 runts, 0 giants, 0 throttles, 0 parity
     843 input errors, 843 CRC, 0 frame, 0 overrun, 0 ignored, 0 abort
     461680525836 packets output, 1378183884420187 bytes, 12 total output drops
     Output 21 broadcast packets, 11050840 multicast packets
     0 output errors, 0 underruns, 0 applique, 0 resets
     0 output buffer failures, 0 output buffers swapped out
     26 carrier transitions


RP/0/RP0/CPU0:rutg-data-sw#"""


def test_reads_one_ports_counters_from_its_interface_output():
    assert parse_port_counters(SHOWN, UPLINK) == {
        "state": "up", "transitions": 26, "cleared": "never", "description": "uplink to star, panel 4",
        "input_errors": 843, "crc": 843, "output_errors": 0, "input_drops": 0, "output_drops": 12}
    down = parse_port_counters(SHOWN.replace("is up, line protocol is up", "is down, line protocol is down"), "Hu0/0/0/23")
    assert down["state"] == "down"
    # Another port's output, two ports at once, or a failed command is not this port's reading.
    assert parse_port_counters(SHOWN, "HundredGigE0/0/0/24") is None
    assert parse_port_counters(SHOWN + SHOWN, UPLINK) is None
    assert parse_port_counters("% Invalid input detected", UPLINK) is None


def _service(name: str, device: str, port: str, vlan: int) -> dict:
    kind, number = ("HundredGigE", port.removeprefix("HundredGigE"))
    return {"name": name, "service_type": "l3rt", "devices": [device],
            "basic_checks": {"status": "up", "checks": [], "intent": {
                "device": device, "interface": [{"type": kind, "id": number, "outervlan": vlan}]}}}


def _scan_case(*, isis: bool = True) -> CaseFile:
    """Two services on one rutg port, one on a lane, and an adjacency over the uplink."""
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = [RUTG, HUB]
    edges = [{"id": f"isis:{HUB}:HundredGigE0/0/0/5.3000:{RUTG}:{UPLINK}.3000", "state": {"status": "up"}}]
    add_evidence(case, {"kind": "spine", "role": "isis", "layer": "underlay",
                        "payload": {"operational_edges": edges if isis else []}})
    services = [_service("a", RUTG, "HundredGigE0/0/0/13", 2109), _service("b", RUTG, "HundredGigE0/0/0/13", 2110),
                _service("c", RUTG, "HundredGigE0/0/0/24/1", 0), _service("d", RUTG, UPLINK, 100)]
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services", "payload": {"extra": {
        "services": {f"l3rt/{s['name']}": s for s in services}}}})
    return case


def test_ports_to_read_are_those_under_a_service_attachment_or_an_adjacency():
    # An uplink has no service attached to it, so it also carries how many services its device holds.
    assert counter_targets(_scan_case()) == {
        (RUTG, "HundredGigE0/0/0/13"): {"services": 2, "adjacency": False},
        (RUTG, "HundredGigE0/0/0/24/1"): {"services": 1, "adjacency": False},
        (RUTG, UPLINK): {"services": 1, "adjacency": True, "device_services": 4},
        (HUB, "HundredGigE0/0/0/5"): {"services": 0, "adjacency": True, "device_services": 0},
    }


def test_a_400g_uplink_is_read_under_its_own_name():
    # As cern-data-sw and newy-data-sw reported their adjacency on 11 October 2026.
    case = CaseFile(budget=Budget(0, 0))
    add_evidence(case, {"kind": "spine", "role": "isis", "layer": "underlay", "payload": {"operational_edges": [
        {"id": "isis:cern-data-sw:FourHundredGigE0/0/0/26.3732:newy-data-sw:FourHundredGigE0/0/0/60.3732"}]}})

    assert sorted(counter_targets(case)) == [
        ("cern-data-sw", "FourHundredGigE0/0/0/26"), ("newy-data-sw", "FourHundredGigE0/0/0/60")]


def _call(outputs: dict, asked: list):
    async def call(client, tool, args):
        assert tool == "exec_show"
        key = (args["device_name"], args["input_command"])
        asked.append(key)
        return {"status": "success", "data": {"result": outputs.get(key, "% Invalid input detected")}}
    return call


def _saved(case: CaseFile) -> dict:
    return next(ev["payload"] for ev in case.evidence if ev.get("kind") == "port_counters")


async def test_scan_reads_each_port_once_and_keeps_what_it_read():
    case, asked = _scan_case(), []
    outputs = {(RUTG, f"interfaces {UPLINK}"): SHOWN}

    await record_port_counters(None, case, ceiling=400, call=_call(outputs, asked))

    assert sorted(asked) == sorted([
        (RUTG, "interfaces HundredGigE0/0/0/13"), (RUTG, "interfaces HundredGigE0/0/0/24/1"),
        (RUTG, f"interfaces {UPLINK}"), (HUB, "interfaces HundredGigE0/0/0/5")])
    saved = _saved(case)
    # A port whose read failed has no reading; that is not a fault.
    assert list(saved["ports"]) == [f"{RUTG}:{UPLINK}"]
    assert saved["ports"][f"{RUTG}:{UPLINK}"] | {"crc": 843, "services": 1, "adjacency": True} == saved["ports"][f"{RUTG}:{UPLINK}"]
    assert (saved["wanted"], saved["left_out"]) == (4, 0) and saved["read_at"].endswith("+00:00")
    assert not [i for i in case.issues if "port_counters" in str(i)]


async def test_ceiling_keeps_the_ports_that_matter_most_and_zero_reads_nothing():
    case, asked = _scan_case(), []
    await record_port_counters(None, case, ceiling=2, call=_call({}, asked))
    # Adjacencies first, then the ports carrying the most services.
    assert sorted(asked) == sorted([(RUTG, f"interfaces {UPLINK}"), (HUB, "interfaces HundredGigE0/0/0/5")])
    assert _saved(case)["left_out"] == 2

    case, asked = _scan_case(), []
    await record_port_counters(None, case, ceiling=0, call=_call({}, asked))
    assert asked == [] and not [ev for ev in case.evidence if ev.get("kind") == "port_counters"]


# --- comparing two scans ---------------------------------------------------------

def _reading(**changed) -> dict:
    return {"state": "up", "transitions": 26, "cleared": "never", "description": "uplink to star, panel 4",
            "input_errors": 0, "crc": 0, "output_errors": 0, "input_drops": 0, "output_drops": 12,
            "services": 0, "adjacency": True, "device_services": 21} | changed


def _counters(read_at: str, **ports) -> dict:
    return {"evidence": [{"kind": "port_counters", "role": "physical", "payload": {
        "read_at": read_at, "ports": {f"{RUTG}:{port}": reading for port, reading in ports.items()}}}]}


EARLIER, LATER = "2026-10-08T03:54:00+00:00", "2026-10-08T05:41:00+00:00"


def _gains(before: dict, now: dict, *, later: str = LATER) -> list[dict]:
    return links_gaining_errors(_counters(EARLIER, **{UPLINK: before}), _counters(later, **{UPLINK: now}))


def test_port_that_is_up_and_gained_errors_since_the_previous_scan_is_a_lead():
    rows = _gains(_reading(), _reading(crc=843, input_errors=843))

    assert rows == [{"device": RUTG, "port": UPLINK, "description": "uplink to star, panel 4",
                     "gained": {"crc": 843, "input_errors": 843}, "state_changes": 0,
                     "minutes": 107, "services": 0, "adjacency": True, "device_services": 21}]


def test_line_says_what_rides_on_the_port():
    from diagnostic_mas.port_counters import gain_line

    row = _gains(_reading(), _reading(crc=5))[0]
    assert gain_line(row | {"description": ""}).startswith("`rutg-data-sw` HundredGigE0/0/0/23: +5 CRC in ")
    assert gain_line(row | {"adjacency": False, "services": 3, "device_services": 0}).endswith("; carries 3 services")
    assert gain_line(row | {"services": 1}).endswith(
        "; carries an IS-IS adjacency and 1 service; 21 services on this device")
    assert gain_line(row | {"minutes": 43, "state_changes": 1, "gained": {}}).split(": ", 1)[1].startswith(
        "1 state change in 43 min")


def test_port_that_flapped_and_came_back_is_a_lead_too():
    rows = _gains(_reading(), _reading(transitions=30))

    assert (rows[0]["gained"], rows[0]["state_changes"]) == ({}, 4)


@pytest.mark.parametrize("before, now", [
    (_reading(), _reading()),                                             # nothing grew
    (_reading(), _reading(output_drops=900, input_drops=40)),             # drops alone are not a lead
    (_reading(), _reading(crc=843, state="down")),                        # already down: the service checks own that
    (_reading(crc=900), _reading(crc=843)),                               # a counter went back: reset
    (_reading(), _reading(crc=843, cleared="00:20:11")),                  # cleared since the previous scan
    (_reading(crc=None), _reading(crc=843, input_errors=None)),           # nothing readable to subtract
])
def test_no_lead_without_growth_on_a_port_that_is_up_and_was_not_reset(before, now):
    assert _gains(before, now) == []


def test_counters_cleared_before_the_previous_scan_can_still_be_compared():
    assert _gains(_reading(cleared="1w2d"), _reading(crc=5, cleared="1w2d"))[0]["gained"] == {"crc": 5}


def test_port_not_read_in_the_previous_scan_has_nothing_to_compare_with():
    assert links_gaining_errors({"evidence": []}, _counters(LATER, **{UPLINK: _reading(crc=843)})) == []
    assert links_gaining_errors(_counters(EARLIER), _counters(LATER, **{UPLINK: _reading(crc=843)})) == []


def test_report_and_notification_list_the_lead_without_touching_any_status():
    older, newer = _counters(EARLIER, **{UPLINK: _reading()}), _counters(LATER, **{UPLINK: _reading(crc=843, input_errors=843)})
    for scan in (older, newer):
        scan.update(issues=[], diagnoses=[], service_coverage={})

    delta = compute_case_delta(older, newer)

    line = ('`rutg-data-sw` HundredGigE0/0/0/23 ("uplink to star, panel 4"): +843 CRC, +843 input errors '
            "in 1 h 47 min; carries an IS-IS adjacency; 21 services on this device")
    text = "\n".join(format_changes_since_previous(delta, previous_run_id="r1"))
    assert f"**Links gaining errors while up:**\n- {line}\n" in text
    assert f"- Links gaining errors while up: 1 — {line}" in digest_changes(delta, "r1", [])
    assert delta["newly_failed_services"] == []


async def test_collection_phase_reads_the_counters_after_the_service_step(monkeypatch):
    from diagnostic_mas import coordinator

    case, asked = CaseFile(budget=Budget(0, 0)), []
    scan = _scan_case()
    spines = {ev["role"]: ev["payload"] for ev in scan.evidence}

    async def service_spine(*args, **kwargs):
        return {"extra": spines["service"]["extra"]}

    async def isis_spine(*args, **kwargs):
        return {"operational_edges": spines["isis"]["operational_edges"]}

    async def bgp_spine(*args, **kwargs):
        return {}

    monkeypatch.setattr(coordinator, "run_service_spine", service_spine)
    monkeypatch.setattr(coordinator, "run_isis_spine", isis_spine)
    monkeypatch.setattr(coordinator, "run_bgp_spine", bgp_spine)
    monkeypatch.setattr("diagnostic_mas.port_counters.call_mcp", _call({(RUTG, f"interfaces {UPLINK}"): SHOWN}, asked))

    await coordinator.run_mandatory_spines(None, None, case, [RUTG, HUB], physical_edges=[], max_port_counter_reads=400)
    assert len(asked) == 4 and case_to_dict(case)["evidence"][-1]["kind"] == "port_counters"

    quiet, asked[:] = CaseFile(budget=Budget(0, 0)), []
    await coordinator.run_mandatory_spines(None, None, quiet, [RUTG, HUB], physical_edges=[], max_port_counter_reads=0)
    assert asked == []
