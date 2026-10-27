"""Error counters on ports that carry a service or an adjacency, compared between scans.

The interface output is as rutg-data-sw returned it for its uplink on 8 October 2026.
A port that is up and gained errors is a lead; it never changes a service's status.
"""
import pytest

from diagnostic_mas.case import Budget, CaseFile, add_evidence
from diagnostic_mas.case_delta import compute_case_delta, format_changes_since_previous
from diagnostic_mas.open_faults import digest_changes
from diagnostic_mas.port_counters import (
    counter_targets, gain_line, links_gaining_errors, parse_port_counters, record_port_counters)
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
        "input_errors": 843, "crc": 843, "output_errors": 0, "input_drops": 0, "output_drops": 12,
        "packets_in": 569938104308, "packets_out": 461680525836}
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
    digest = "\n".join(digest_changes(delta, "r1", []))
    assert f"\n\n**Links gaining errors while up:** 1\n- {line}\n\n**Still open:** none" in digest
    assert delta["newly_failed_services"] == []


# What five ports gained between the scans of 11 and 12 October 2026.
TOKY, FLAPPED, EDUKY = "TenGigE0/0/0/18/0", "HundredGigE0/0/0/22", "HundredGigE0/0/0/7"
SECOND_FLAP = "HundredGigE0/0/0/21"


def _five_leads() -> tuple[dict, dict]:
    ports = (EDUKY, FLAPPED, SECOND_FLAP, UPLINK, TOKY)
    now = {EDUKY: _reading(crc=1, input_errors=1), FLAPPED: _reading(transitions=28),
           SECOND_FLAP: _reading(transitions=28), UPLINK: _reading(crc=1, input_errors=1),
           TOKY: _reading(crc=57190, input_errors=57190)}
    return _counters(EARLIER, **{port: _reading() for port in ports}), _counters(LATER, **now)


def test_the_link_that_gained_most_is_listed_first_then_the_ones_that_only_changed_state():
    rows = links_gaining_errors(*_five_leads())

    assert [row["port"] for row in rows] == [TOKY, UPLINK, EDUKY, SECOND_FLAP, FLAPPED]


def test_notification_gives_the_three_largest_one_per_line():
    older, newer = _five_leads()
    for scan in (older, newer):
        scan.update(issues=[], diagnoses=[], service_coverage={})

    lines = digest_changes(compute_case_delta(older, newer), "r1", [])

    start = lines.index("**Links gaining errors while up:** 5, largest first")
    # The fixture's ports carry a description, printed after the port name.
    assert [line.split(" (")[0] for line in lines[start + 1:start + 5]] == [
        f"- `rutg-data-sw` {TOKY}", f"- `rutg-data-sw` {UPLINK}", f"- `rutg-data-sw` {EDUKY}", "- …and 2 more"]
    assert ": +57190 CRC, +57190 input errors in 1 h 47 min" in lines[start + 1]
    assert lines[start - 1] == "" and lines[start + 5] == "" and lines[start + 6].startswith("**Still open:**")
    assert not [line for line in lines if line.startswith("- Links gaining errors")]


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


# --- a device without a CLI exec action (Ciena) ---------------------------------

CIEN = "cien-data-sw"


def _ciena_case() -> CaseFile:
    """One service on cien port 14, which the service model names Generic 14."""
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = [CIEN]
    service = {"name": "lan", "service_type": "l2bridge", "devices": [CIEN],
               "basic_checks": {"status": "unknown", "checks": [], "intent": {
                   "device": CIEN, "interface": [{"type": "Generic", "id": "14", "outervlan": 2008}]}}}
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services",
                        "payload": {"extra": {"services": {"l2bridge/lan": service}}}})
    return case


async def test_port_on_a_device_without_cli_is_read_through_get_interface_health():
    asked = []

    async def call(client, tool, args):
        asked.append((tool, dict(args)))
        if tool == "exec_show":
            return {"status": "error", "error_code": "exec_unavailable", "error_message": "no CLI exec action"}
        return {"status": "success", "data": {"platform": "saos", "interfaces": [
            {"name": "14", "description": "14", "admin_status": "up", "oper_status": "up", "state_changes": None,
             "link_flap_events": 0, "input_errors": 7, "crc_errors": 3, "read_ok": True}]}}

    case = _ciena_case()
    await record_port_counters(None, case, ceiling=400, call=call)

    # The service model calls the port Generic14; the device calls it 14.
    assert asked == [("exec_show", {"device_name": CIEN, "input_command": "interfaces Generic14"}),
                     ("get_interface_health", {"device_name": CIEN, "interface": "14"})]
    reading = _saved(case)["ports"][f"{CIEN}:Generic14"]
    assert reading | {"state": "up", "input_errors": 7, "crc": 3, "transitions": None, "cleared": None,
                      "source": "get_interface_health", "services": 1} == reading


async def test_a_port_that_failed_for_another_reason_is_not_retried_through_the_structured_tool():
    asked = []

    async def call(client, tool, args):
        asked.append(tool)
        return {"status": "error", "error_code": "nso_error", "error_message": "southbound locked"}

    case = _ciena_case()
    await record_port_counters(None, case, ceiling=400, call=call)
    assert asked == ["exec_show"] and _saved(case)["ports"] == {}


def test_structured_readings_compare_without_a_clear_time_and_skip_a_counter_that_went_back():
    def ciena(**changed):
        return {"state": "up", "transitions": None, "cleared": None, "description": "14", "input_errors": 7,
                "crc": 3, "output_errors": None, "input_drops": None, "output_drops": None,
                "source": "get_interface_health", "services": 1, "adjacency": False} | changed

    assert _gains(ciena(), ciena(crc=10, input_errors=14))[0]["gained"] == {"crc": 7, "input_errors": 7}
    assert _gains(ciena(crc=10), ciena(crc=4)) == []
    # An IOS-XR reading without a clear time is still not compared.
    assert _gains(_reading(cleared=None), _reading(crc=843, cleared=None)) == []


# --- a Ciena port that went down and came back between scans --------------------
# Values as cien-data-sw reported port 14 on 19 October 2026: last change 3 Aug when test44 read it
# at 16:33 UTC, and 19 Oct 18:21:30 UTC (a restart) when test45 read it at 20:30 UTC.

AUG_3, OCT_19_1821 = "1785773106236775980", "1792434090733640352"


async def test_structured_reading_keeps_the_last_change_as_an_absolute_time():
    from diagnostic_mas.port_counters import _structured_reading

    def answer(raw):
        async def call(client, tool, args):
            return {"status": "success", "data": {"interfaces": [
                {"name": "14", "admin_status": "up", "oper_status": "up", "input_errors": 0, "crc_errors": 0,
                 "last_change": {"raw": raw}, "read_ok": True}]}}
        return call

    assert (await _structured_reading(answer(OCT_19_1821), None, CIEN, "Generic14"))["changed_at"] == "2026-10-19T18:21:30+00:00"
    assert (await _structured_reading(answer("1792434090"), None, CIEN, "Generic14"))["changed_at"] == "2026-10-19T18:21:30+00:00"
    assert (await _structured_reading(answer("never"), None, CIEN, "Generic14"))["changed_at"] is None


def _ciena_reading(changed_at, **changed):
    return {"state": "up", "transitions": None, "cleared": None, "description": "14", "input_errors": 0, "crc": 0,
            "output_errors": None, "input_drops": None, "output_drops": None, "changed_at": changed_at,
            "source": "get_interface_health", "services": 1, "adjacency": False} | changed


def _ciena_scans(before: dict, now: dict) -> list[dict]:
    def scan(read_at, reading):
        return {"evidence": [{"kind": "port_counters", "payload": {"read_at": read_at,
                                                                  "ports": {f"{CIEN}:Generic14": reading}}}]}
    return links_gaining_errors(scan("2026-10-19T16:33:00+00:00", before), scan("2026-10-19T20:30:00+00:00", now))


def test_ciena_port_whose_last_change_is_after_the_previous_scan_went_down_and_came_back():
    rows = _ciena_scans(_ciena_reading("2026-08-03T16:05:06+00:00"), _ciena_reading("2026-10-19T18:21:30+00:00"))

    assert [(r["port"], r["gained"], r["state_changes"], r["bounced"]) for r in rows] == [("Generic14", {}, 0, True)]
    assert gain_line(rows[0]).startswith(
        '`cien-data-sw` Generic14 ("14"): went down and came back since the previous scan (count not reported) in 3 h 57 min')


@pytest.mark.parametrize("before, now", [
    (_ciena_reading("2026-08-03T16:05:06+00:00"), _ciena_reading("2026-08-03T16:05:06+00:00")),     # no change
    (_ciena_reading("2026-08-03T16:05:06+00:00"), _ciena_reading("2026-10-19T18:21:30+00:00", state="down")),
    (_ciena_reading(None), _ciena_reading(None)),                                                    # time not given
])
def test_no_bounce_without_a_later_change_on_a_port_that_is_up(before, now):
    assert _ciena_scans(before, now) == []


# --- one line per link: both ends of an IS-IS adjacency together ------------------

def _two_scans(edges: list[str], **changed_by_port) -> tuple[dict, dict]:
    """Earlier and later scans: every port starts clean; ``changed_by_port`` holds the later readings."""
    def scan(read_at: str, ports: dict) -> dict:
        return {"evidence": [
            {"kind": "port_counters", "role": "physical", "payload": {"read_at": read_at, "ports": ports}},
            {"kind": "spine", "role": "isis", "payload": {"operational_edges": [{"id": e} for e in edges]}}]}
    before = {key: _reading(description="") for key in changed_by_port}
    later = {key: _reading(description="") | changed for key, changed in changed_by_port.items()}
    return scan(EARLIER, before), scan(LATER, later)


def test_the_two_ends_of_a_link_are_one_line_and_a_lone_end_names_its_far_end():
    from diagnostic_mas.port_counters import gain_lines
    from nso_facts.topology.graph import isis_edge_id

    salt, kans, toky, seat = "salt-data-sw", "kans-data-sw", "toky-data-sw", "seat-data-sw"
    edges = [isis_edge_id(salt, "HundredGigE0/0/0/17.3000", kans, "HundredGigE0/0/0/23.3000"),
             isis_edge_id(seat, "HundredGigE0/0/0/19.1491", toky, "TenGigE0/0/0/18/0.1491"),
             isis_edge_id(salt, "?", seat, "HundredGigE0/0/0/23.3000")]
    rows = links_gaining_errors(*_two_scans(edges, **{
        f"{salt}:HundredGigE0/0/0/17": {"crc": 5, "input_errors": 20, "transitions": 44, "device_services": 74},
        f"{kans}:HundredGigE0/0/0/23": {"crc": 7, "input_errors": 9, "transitions": 44, "device_services": 208},
        f"{toky}:TenGigE0/0/0/18/0": {"crc": 95704, "input_errors": 95704, "device_services": 53},
        f"{seat}:HundredGigE0/0/0/23": {"transitions": 30, "device_services": 39},
        f"{salt}:HundredGigE0/0/0/5": {"crc": 2, "adjacency": False, "services": 3, "device_services": 0}}))

    assert len(rows) == 5 and all(row["port"] for row in rows)            # per port still, for Grafana
    assert gain_lines(rows) == [
        f"`{toky}` TenGigE0/0/0/18/0 (IS-IS link to `{seat}` HundredGigE0/0/0/19.1491): "
        "+95704 CRC, +95704 input errors in 1 h 47 min; 53 services on this device",
        f"`{salt}` HundredGigE0/0/0/17 ↔ `{kans}` HundredGigE0/0/0/23, one IS-IS link, in 1 h 47 min: "
        "salt +5 CRC, +20 input errors, 18 state changes; kans +7 CRC, +9 input errors, 18 state changes. "
        "74 and 208 services on these devices.",
        f"`{salt}` HundredGigE0/0/0/5: +2 CRC in 1 h 47 min; carries 3 services",
        f"`{seat}` HundredGigE0/0/0/23 (IS-IS link to `{salt}`): 4 state changes in 1 h 47 min; "
        "39 services on this device",
    ]


def test_the_changes_section_counts_links_not_ports():
    from diagnostic_mas.case_delta import format_changes_since_previous
    from nso_facts.topology.graph import isis_edge_id

    edge = isis_edge_id("salt-data-sw", "HundredGigE0/0/0/17.3000", "kans-data-sw", "HundredGigE0/0/0/23.3000")
    rows = links_gaining_errors(*_two_scans([edge], **{
        "salt-data-sw:HundredGigE0/0/0/17": {"transitions": 44}, "kans-data-sw:HundredGigE0/0/0/23": {"transitions": 44}}))
    text = "\n".join(format_changes_since_previous({"links_gaining_errors": rows}))
    assert text.count("one IS-IS link") == 1 and "…and" not in text
