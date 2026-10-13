"""Error counters on the ports that carry a service or an IS-IS adjacency.

Each scan reads them once and keeps them; the next scan subtracts. A port that
is up and gained errors since the previous scan is reported as a lead. It never
sets or changes a service's status, and a read that fails is not a fault.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from diagnostic_mas.case import CaseFile, add_evidence
from nso_facts.mcp_client import call_mcp
from nso_facts.topology.interfaces import interface_variants, interfaces_match

KIND = "port_counters"
_HEAD = (r"^([\w/.-]+) is (administratively down|up|down), "
         r"line protocol is (administratively down|up|down)")
_NUMBERS = {
    "transitions": r"^\s+Interface state transitions: (\d+)",
    "input_drops": r"(\d+) total input drops",
    "input_errors": r"^\s+(\d+) input errors,",
    "crc": r"^\s+\d+ input errors, (\d+) CRC",
    "output_drops": r"(\d+) total output drops",
    "output_errors": r"^\s+(\d+) output errors,",
}
_ERRORS = {"crc": "CRC", "input_errors": "input errors", "output_errors": "output errors"}


def parse_port_counters(text: str, port: str) -> dict[str, Any] | None:
    """The one port's state and counters from ``show interfaces <port>``; None if the answer is not about it."""
    heads = re.findall(_HEAD, text or "", re.M | re.I)
    if len(heads) != 1 or not interfaces_match(port, heads[0][0]):
        return None
    admin, line = heads[0][1].lower(), heads[0][2].lower()
    reading: dict[str, Any] = {"state": "up" if admin == line == "up" else "admin-down" if "admin" in admin else "down"}
    for name, pattern in _NUMBERS.items():
        found = re.search(pattern, text, re.M)
        reading[name] = int(found[1]) if found else None
    cleared = re.search(r'^\s+Last clearing of "show interface" counters (\S+)', text, re.M)
    described = re.search(r"^\s+Description: (.+?)\s*$", text, re.M)
    reading["cleared"] = cleared[1] if cleared else None
    reading["description"] = described[1][:120] if described else ""
    return reading


def _parent_port(interface: str) -> str:
    """The physical port or lane under an attachment or adjacency, in the device's long spelling."""
    from diagnostic_mas.port_investigation import _link_of

    name = _link_of(interface)
    return max(interface_variants(name) or {name}, key=len)


def counter_targets(case: CaseFile) -> dict[tuple[str, str], dict[str, Any]]:
    """(device, port) -> what rides on it, for every port under a service attachment or an IS-IS adjacency."""
    from diagnostic_mas.operational_checks.probe import attachments

    riders: dict[tuple[str, str], set[str]] = {}
    on_device: dict[str, set[str]] = {}
    adjacent: set[tuple[str, str]] = set()
    for ev in case.evidence:
        if ev.get("kind") != "spine":
            continue
        payload = ev.get("payload") or {}
        if ev.get("role") == "service":
            for record in ((payload.get("extra") or {}).get("services") or {}).values():
                intent = (record.get("basic_checks") or {}).get("intent") if isinstance(record, dict) else None
                for end in attachments(intent or {}):
                    riders.setdefault((end["device"], _parent_port(end["ac"])), set()).add(str(record.get("name")))
                    on_device.setdefault(end["device"], set()).add(str(record.get("name")))
        elif ev.get("role") == "isis":
            for edge in payload.get("operational_edges") or []:
                parts = str((edge or {}).get("id") or "").split(":")
                if len(parts) == 5:
                    adjacent.update({(parts[1], _parent_port(parts[2])), (parts[3], _parent_port(parts[4]))})
    # Services attach to access ports, not to an uplink: an uplink says how many its device holds.
    return {key: {"services": len(riders.get(key, ())), "adjacency": key in adjacent}
            | ({"device_services": len(on_device.get(key[0], ()))} if key in adjacent else {})
            for key in sorted(riders.keys() | adjacent)}


async def record_port_counters(client: Any, case: CaseFile, *, ceiling: int, workers: int = 1, call: Any = None) -> None:
    """Read each target port once, within ``ceiling``, and keep the readings with the scan."""
    targets = counter_targets(case)
    if ceiling <= 0 or not targets:
        return
    from diagnostic_mas.operational_checks.probe import Probe, safe
    from diagnostic_mas.operational_checks.scheduler import DeviceCalls, ProbeCache, run_groups

    # Under a ceiling, uplinks come first, then the ports carrying the most services.
    order = sorted(targets, key=lambda key: (not targets[key]["adjacency"], -targets[key]["services"], key))
    by_device: dict[str, list[str]] = {}
    for device, port in order[:ceiling]:
        if safe(device) and safe(port):
            by_device.setdefault(device, []).append(port)
    calls, cache = DeviceCalls(call or call_mcp, max(1, workers)), ProbeCache()
    ports: dict[str, dict[str, Any]] = {}

    async def read(device: str) -> None:
        probe = Probe(client, calls, cache, max_calls=len(by_device[device]))
        for port in by_device[device]:
            reading = parse_port_counters(await probe.show(device, f"interfaces {port}", required=False), port)
            if reading:
                ports[f"{device}:{port}"] = reading | targets[device, port]

    await run_groups(sorted(by_device), read, lambda device: device, max(1, workers))
    add_evidence(case, {"kind": KIND, "role": "physical", "layer": "physical", "payload": {
        "read_at": datetime.now(timezone.utc).isoformat(), "ports": dict(sorted(ports.items())),
        "wanted": len(targets), "left_out": max(0, len(order) - ceiling)}})


def _saved(case: dict[str, Any]) -> dict[str, Any]:
    found = [ev.get("payload") or {} for ev in case.get("evidence") or []
             if isinstance(ev, dict) and ev.get("kind") == KIND]
    return found[-1] if found else {}


def _age_seconds(text: Any) -> int | None:
    """Seconds in a device uptime such as ``00:20:11``, ``3d04h``, ``1w2d`` or ``1y12w``."""
    text = str(text or "")
    clock = re.fullmatch(r"(\d+):(\d{2}):(\d{2})", text)
    if clock:
        return int(clock[1]) * 3600 + int(clock[2]) * 60 + int(clock[3])
    parts = re.fullmatch(r"(?:(\d+)y)?(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?", text)
    if not text or not parts:
        return None
    years, weeks, days, hours = (int(part or 0) for part in parts.groups())
    return ((years * 365 + weeks * 7 + days) * 24 + hours) * 3600


def links_gaining_errors(older: dict[str, Any], newer: dict[str, Any]) -> list[dict[str, Any]]:
    """Ports that are up now and gained errors, or changed state and came back, since the previous scan."""
    before, now = _saved(older), _saved(newer)
    try:
        elapsed = (datetime.fromisoformat(now["read_at"]) - datetime.fromisoformat(before["read_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        elapsed = None
    rows = []
    for key, reading in (now.get("ports") or {}).items():
        earlier = (before.get("ports") or {}).get(key)
        if not earlier or reading.get("state") != "up":
            continue

        def both(name: str) -> tuple[int, int] | None:
            pair = earlier.get(name), reading.get(name)
            return pair if all(isinstance(value, int) for value in pair) else None

        # Counters cleared since the previous scan, or one that went back, cannot be subtracted.
        if reading.get("cleared") != "never":
            age = _age_seconds(reading.get("cleared"))
            if age is None or elapsed is None or age < elapsed:
                continue
        if any(pair and pair[1] < pair[0] for pair in map(both, (*_NUMBERS,))):
            continue
        gained = {name: pair[1] - pair[0] for name in _ERRORS if (pair := both(name)) and pair[1] > pair[0]}
        moved = both("transitions")
        state_changes = moved[1] - moved[0] if moved else 0
        if not gained and not state_changes:
            continue
        device, _, port = key.partition(":")
        rows.append({"device": device, "port": port, "description": reading.get("description") or "",
                     "gained": gained, "state_changes": state_changes,
                     "minutes": round(elapsed / 60) if elapsed is not None else None,
                     "services": int(reading.get("services") or 0), "adjacency": bool(reading.get("adjacency")),
                     "device_services": int(reading.get("device_services") or 0)})
    # Most errors gained first, then links that only changed state: a short list must not cut off the worst one.
    return sorted(rows, key=lambda row: (-max(row["gained"].values(), default=0), -row["state_changes"],
                                         row["device"], row["port"]))


def gain_line(row: dict[str, Any]) -> str:
    parts = [f"+{row['gained'][name]} {label}" for name, label in _ERRORS.items() if name in row["gained"]]
    if row["state_changes"]:
        parts.append(f"{row['state_changes']} state change{'' if row['state_changes'] == 1 else 's'}")
    minutes = row.get("minutes")
    took = "" if minutes is None else f" in {minutes // 60} h {minutes % 60} min" if minutes >= 60 else f" in {minutes} min"
    services = row["services"]
    carries = " and ".join(part for part in (
        "an IS-IS adjacency" if row["adjacency"] else "",
        f"{services} service{'' if services == 1 else 's'}" if services else "") if part)
    held = row.get("device_services") or 0
    described = f' ("{row["description"]}")' if row.get("description") else ""
    return (f"`{row['device']}` {row['port']}{described}: {', '.join(parts)}{took}"
            + (f"; carries {carries}" if carries else "")
            + (f"; {held} service{'' if held == 1 else 's'} on this device" if row["adjacency"] and held else ""))
