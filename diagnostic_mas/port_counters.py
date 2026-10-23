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
            command = f"interfaces {port}"
            reading = parse_port_counters(await probe.show(device, command, required=False), port)
            if reading is None and probe.error_codes.get((device, command)) == "exec_unavailable":
                reading = await _structured_reading(calls, client, device, port)
            if reading:
                ports[f"{device}:{port}"] = reading | targets[device, port]

    await run_groups(sorted(by_device), read, lambda device: device, max(1, workers))
    add_evidence(case, {"kind": KIND, "role": "physical", "layer": "physical", "payload": {
        "read_at": datetime.now(timezone.utc).isoformat(), "ports": dict(sorted(ports.items())),
        "wanted": len(targets), "left_out": max(0, len(order) - ceiling)}})


async def _structured_reading(call: Any, client: Any, device: str, port: str) -> dict[str, Any] | None:
    """The port's state and error counters from ``get_interface_health``, for a device without CLI.

    The service model names such a port by type and id (``Generic14``); the device names it by
    its id. There is no clear time and no state-transition count, so those stay None.
    """
    named = re.fullmatch(r"Generic(\S+)", port)
    result = await call(client, "get_interface_health", {"device_name": device, "interface": named[1] if named else port})
    data = result.get("data") if isinstance(result, dict) and result.get("status") == "success" else None
    rows = [row for row in (data or {}).get("interfaces") or [] if isinstance(row, dict) and row.get("read_ok") is not False]
    if len(rows) != 1:
        return None
    row = rows[0]
    admin, oper = str(row.get("admin_status") or "").lower(), str(row.get("oper_status") or "").lower()
    if admin not in ("up", "down") or not oper:
        return None
    counts = {name: row.get(field) if isinstance(row.get(field), int) else None
              for name, field in (("input_errors", "input_errors"), ("crc", "crc_errors"))}
    return {"state": "admin-down" if admin == "down" else "up" if oper == "up" else "down",
            "transitions": None, "input_drops": None, "output_drops": None, "output_errors": None, **counts,
            "cleared": None, "description": str(row.get("description") or "")[:120],
            "changed_at": _change_time(row.get("last_change")), "source": "get_interface_health"}


def _change_time(last_change: Any) -> str | None:
    """The device's last-change stamp as UTC time; 19 digits are nanoseconds, 10 are seconds."""
    raw = str(last_change.get("raw") if isinstance(last_change, dict) else last_change or "").strip()
    if re.fullmatch(r"\d{19}", raw):
        seconds = int(raw) // 10**9
    elif re.fullmatch(r"\d{10}", raw):
        seconds = int(raw)
    else:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


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


def _far_ends(case: dict[str, Any]) -> dict[tuple[str, str], dict[str, str]]:
    """(device, port) -> the other end of the IS-IS adjacency on that port, from the scan's IS-IS collection."""
    out: dict[tuple[str, str], dict[str, str]] = {}
    for ev in case.get("evidence") or []:
        if not isinstance(ev, dict) or ev.get("kind") != "spine" or ev.get("role") != "isis":
            continue
        for edge in (ev.get("payload") or {}).get("operational_edges") or []:
            parts = str((edge or {}).get("id") or "").split(":")
            if len(parts) != 5:
                continue
            ends = ((parts[1], parts[2]), (parts[3], parts[4]))
            for (device, interface), (far, far_interface) in (ends, ends[::-1]):
                if interface != "?":
                    out.setdefault((device, _parent_port(interface)), {"device": far, "interface": far_interface})
    return out


def links_gaining_errors(older: dict[str, Any], newer: dict[str, Any]) -> list[dict[str, Any]]:
    """Ports that are up now and gained errors, or changed state and came back, since the previous scan.

    One row per port, the far end of its IS-IS adjacency attached; ``gain_lines`` puts a link's ends together.
    """
    before, now = _saved(older), _saved(newer)
    far_ends = _far_ends(newer)
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
        # A structured reading has no clear time; the went-back check below still catches a
        # reset, and a missed one could only make the gain smaller, never invent one.
        structured = reading.get("source") == earlier.get("source") == "get_interface_health"
        if reading.get("cleared") != "never" and not structured:
            age = _age_seconds(reading.get("cleared"))
            if age is None or elapsed is None or age < elapsed:
                continue
        if any(pair and pair[1] < pair[0] for pair in map(both, (*_NUMBERS,))):
            continue
        gained = {name: pair[1] - pair[0] for name in _ERRORS if (pair := both(name)) and pair[1] > pair[0]}
        moved = both("transitions")
        state_changes = moved[1] - moved[0] if moved else 0
        # Without a transition count, a last change after the previous read means it went down and back.
        bounced = False
        if structured and not state_changes:
            try:
                bounced = datetime.fromisoformat(str(reading.get("changed_at"))) > datetime.fromisoformat(before["read_at"])
            except (KeyError, TypeError, ValueError):
                bounced = False
        if not gained and not state_changes and not bounced:
            continue
        device, _, port = key.partition(":")
        far = far_ends.get((device, port)) if reading.get("adjacency") else None
        rows.append({"device": device, "port": port, "description": reading.get("description") or "",
                     "gained": gained, "state_changes": state_changes, **({"bounced": True} if bounced else {}),
                     **({"far_end": far} if far else {}),
                     "minutes": round(elapsed / 60) if elapsed is not None else None,
                     "services": int(reading.get("services") or 0), "adjacency": bool(reading.get("adjacency")),
                     "device_services": int(reading.get("device_services") or 0)})
    # Most errors gained first, then links that only changed state: a short list must not cut off the worst one.
    return sorted(rows, key=lambda row: (-max(row["gained"].values(), default=0), -row["state_changes"],
                                         row["device"], row["port"]))


def _changes(row: dict[str, Any], *, bounced: str) -> list[str]:
    parts = [f"+{row['gained'][name]} {label}" for name, label in _ERRORS.items() if name in row["gained"]]
    if row["state_changes"]:
        parts.append(f"{row['state_changes']} state change{'' if row['state_changes'] == 1 else 's'}")
    elif row.get("bounced"):
        parts.append(bounced)
    return parts


def _took(row: dict[str, Any]) -> str:
    minutes = row.get("minutes")
    return "" if minutes is None else f" in {minutes // 60} h {minutes % 60} min" if minutes >= 60 else f" in {minutes} min"


def gain_line(row: dict[str, Any]) -> str:
    parts = _changes(row, bounced="went down and came back since the previous scan (count not reported)")
    services = row["services"]
    far = row.get("far_end") or {}
    carries = " and ".join(part for part in (
        "an IS-IS adjacency" if row["adjacency"] and not far else "",
        f"{services} service{'' if services == 1 else 's'}" if services else "") if part)
    held = row.get("device_services") or 0
    described = f' ("{row["description"]}")' if row.get("description") else ""
    if far:
        towards = f"`{far['device']}`" + (f" {far['interface']}" if far.get("interface") not in (None, "?") else "")
        described += f" (IS-IS link to {towards})"
    return (f"`{row['device']}` {row['port']}{described}: {', '.join(parts)}{_took(row)}"
            + (f"; carries {carries}" if carries else "")
            + (f"; {held} service{'' if held == 1 else 's'} on this device" if row["adjacency"] and held else ""))


def gain_lines(rows: list[dict[str, Any]]) -> list[str]:
    """One line per link, worst first: the two ends of an IS-IS link together when both are listed."""
    listed = {(row["device"], row["port"]): row for row in rows}
    done: set[tuple[str, str]] = set()
    lines = []
    for row in rows:
        key = (row["device"], row["port"])
        if key in done:
            continue
        done.add(key)
        far = row.get("far_end") or {}
        other_key = (far.get("device"), _parent_port(far["interface"])) if far.get("interface") not in (None, "?") else None
        other = listed.get(other_key) if other_key not in done else None
        if other is None:
            lines.append(gain_line(row))
            continue
        done.add(other_key)
        short = lambda r: str(r["device"]).split("-", 1)[0]
        ends = "; ".join(f"{short(r)} {', '.join(_changes(r, bounced='went down and came back'))}" for r in (row, other))
        held = [r.get("device_services") or 0 for r in (row, other)]
        took = _took(row)
        lines.append(f"`{row['device']}` {row['port']} ↔ `{other['device']}` {other['port']}, one IS-IS link"
                     f"{',' + took if took else ''}: {ends}."
                     + (f" {held[0]} and {held[1]} services on these devices." if any(held) else ""))
    return lines
