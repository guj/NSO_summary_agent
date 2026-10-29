"""Packets each end of an IS-IS adjacency down sent and received since the previous scan."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from diagnostic_mas.case import add_evidence
from diagnostic_mas.drill import prior_exploration_for_issue
from diagnostic_mas.link_lldp import record_link_lldp
from diagnostic_mas.link_traffic import record_link_traffic, traffic_line
from diagnostic_mas.operator_report import format_devices_operator
from diagnostic_mas.port_counters import counter_targets, parse_port_counters, record_port_counters
from test_link_lldp import EDGE, OUTPUTS, SALT, SEAT, _call, _case, _table
from test_port_counters import SHOWN

SALT_PORT, SEAT_PORT = "HundredGigE0/0/0/21", "HundredGigE0/0/0/23"


def _shown(port: str, packets_in: int, packets_out: int, bytes_in: int = 1315276986914302,
           bytes_out: int = 1378183884420187) -> str:
    """``show interfaces <port>`` with these counts (rutg's output of 8 October 2026 otherwise)."""
    return (SHOWN.replace("HundredGigE0/0/0/23 is up", f"{port} is up")
            .replace("569938104308 packets input, 1315276986914302 bytes",
                     f"{packets_in} packets input, {bytes_in} bytes")
            .replace("461680525836 packets output, 1378183884420187 bytes",
                     f"{packets_out} packets output, {bytes_out} bytes"))


async def _scanned(salt: tuple[int, ...] | None, seat: tuple[int, ...] | None, previous: dict | None,
                   lldp: dict | None = None):
    """A scan of the salt–seat adjacency: LLDP, then each end's counters, then the comparison."""
    case = _case()
    add_evidence(case, {"kind": "spine", "role": "isis", "layer": "underlay",
                        "payload": {"operational_edges": [{"id": EDGE, "state": {"status": "down"}}]}})
    case.previous_port_counters = previous or {}
    outputs = {**OUTPUTS, **(lldp or {})}
    if salt:
        outputs[SALT, f"interfaces {SALT_PORT}"] = _shown(SALT_PORT, *salt)
    if seat:
        outputs[SEAT, f"interfaces {SEAT_PORT}"] = _shown(SEAT_PORT, *seat)
    await record_link_lldp(None, case, call=_call(outputs, []))
    await record_port_counters(None, case, ceiling=10, call=_call(outputs, []))
    record_link_traffic(case)
    return case


def _before(**ports: tuple[int, ...]) -> dict:
    """The previous scan's counters, 11 h 20 min ago: (packets in, packets out[, bytes in, bytes out]) per end."""
    read_at = (datetime.now(timezone.utc) - timedelta(hours=11, minutes=20)).isoformat()
    names = {"salt": f"{SALT}:{SALT_PORT}", "seat": f"{SEAT}:{SEAT_PORT}"}
    return {"read_at": read_at, "ports": {names[end]: {"state": "up", "cleared": "never"}
                                          | dict(zip(("packets_in", "packets_out", "bytes_in", "bytes_out"), counts))
                                          for end, counts in ports.items()}}


def test_packet_counts_are_read_with_the_error_counters():
    reading = parse_port_counters(_shown(SALT_PORT, 1038504, 465806), SALT_PORT)
    assert (reading["packets_in"], reading["packets_out"]) == (1038504, 465806)


@pytest.mark.asyncio
async def test_the_port_lldp_named_has_its_counters_read():
    case = await _scanned((1250, 1280), (1975, 1234572890), None)

    targets = counter_targets(case)
    assert (SALT, SALT_PORT) in targets and (SEAT, SEAT_PORT) in targets
    assert not any(port == "?" for _, port in targets)


@pytest.mark.asyncio
async def test_each_direction_sets_what_one_end_sent_against_what_the_other_received():
    case = await _scanned((1250, 1280), (1975, 1234572890), _before(salt=(200, 300), seat=(1000, 5000)))

    assert traffic_line(case, EDGE) == (
        f"Packets since the previous scan (11 h 20 min): {SALT} → {SEAT}: 980 sent, 975 received; "
        f"{SEAT} → {SALT}: 1,234,567,890 sent, 1,050 received.")


@pytest.mark.asyncio
async def test_with_another_device_on_the_link_each_port_s_counts_stand_alone():
    # salt's port hears utah, not seat: what it took in did not all come from seat.
    salt = _table("Mon Oct 26 20:08:10.524 UTC", [
        "utah-data-sw.fabric. HundredGigE0/0/0/21             120        R               HundredGigE0/0/0/23"])
    case = await _scanned((1250, 1280), (1975, 1234572890), _before(salt=(200, 300), seat=(1000, 5000)),
                          lldp={(SALT, "lldp neighbors"): salt})

    assert traffic_line(case, EDGE) == (
        f"Packets since the previous scan (11 h 20 min), each port on its own (LLDP shows another device on the "
        f"link, so one end is not set against the other): {SALT}: 1,050 in, 980 out; {SEAT}: 975 in, "
        "1,234,567,890 out.")

    first = await _scanned((1250, 1280), (1975, 1234572890), _before(seat=(1000, 5000)),
                           lldp={(SALT, "lldp neighbors"): salt})
    assert traffic_line(first, EDGE).endswith(f"{SALT}: no earlier reading; {SEAT}: 975 in, 1,234,567,890 out.")


@pytest.mark.asyncio
async def test_bytes_go_beside_the_packets_when_both_scans_counted_them():
    # Here seat → salt received 4 bytes per frame more than was sent, as a tag added on the way would show.
    case = await _scanned((1190, 1280, 86_960, 81_000), (1980, 2890, 91_000, 99_000),
                          _before(salt=(200, 300, 10_000, 20_000), seat=(1000, 1900, 30_000, 26_000)))

    assert traffic_line(case, EDGE) == (
        f"Packets since the previous scan (11 h 20 min): {SALT} → {SEAT}: 980 sent, 980 received "
        f"(bytes: 61,000 sent, 61,000 received); {SEAT} → {SALT}: 990 sent, 990 received "
        f"(bytes: 73,000 sent, 76,960 received).")

    # A previous scan saved before bytes were kept still compares packets.
    older = await _scanned((1190, 1280, 86_960, 81_000), (1980, 2890, 91_000, 99_000),
                           _before(salt=(200, 300), seat=(1000, 1900)))
    assert traffic_line(older, EDGE) == (
        f"Packets since the previous scan (11 h 20 min): {SALT} → {SEAT}: 980 sent, 980 received; "
        f"{SEAT} → {SALT}: 990 sent, 990 received.")


@pytest.mark.asyncio
async def test_a_port_without_an_earlier_reading_or_with_restarted_counters_is_not_compared():
    # salt's port was first read in this scan; seat's counters went back (cleared or restarted).
    case = await _scanned((1250, 1280), (10, 20), _before(seat=(1000, 5000)))

    assert traffic_line(case, EDGE) == (
        f"Packets since the previous scan (11 h 20 min): {SALT} → {SEAT}: not compared (no earlier reading on "
        f"{SALT}; counters restarted on {SEAT}); {SEAT} → {SALT}: not compared (counters restarted on {SEAT}; "
        f"no earlier reading on {SALT}).")

    first = await _scanned((1250, 1280), (1975, 1234572890), None)
    assert traffic_line(first, EDGE) == "Packets: no earlier scan to compare with."


@pytest.mark.asyncio
async def test_the_report_and_the_drill_get_the_packet_line():
    case = await _scanned((1250, 1280), (1975, 1234572890), _before(salt=(200, 300), seat=(1000, 5000)))
    line = traffic_line(case, EDGE)

    text = "\n".join(format_devices_operator(case))
    assert line in text.split(f"### {SALT}\n", 1)[1].split("\n### ", 1)[0]
    assert line in prior_exploration_for_issue(case, case.issues[0])["link_facts"]["readings"]
