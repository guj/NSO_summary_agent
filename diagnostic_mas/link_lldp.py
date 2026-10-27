"""LLDP at both ends of an IS-IS adjacency that is down although both routers answered.

Read once per scan, after the spines and before the optics: each end router's IOS-XR
``show lldp neighbors``. An entry on router X records frames X received from the listed
neighbor, so each direction is told by the receiving end's table, never the sending end's.
When the adjacency names only one end's interface, the other end's table names the missing
port, so the optics read can reach it. A table that was not read is "not read", never
"not heard"; "not heard" is a reading, not a cause.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from diagnostic_mas.case import CaseFile, add_evidence
from diagnostic_mas.link_optics import _ADJACENCY_DOWN, _ends
from nso_facts.mcp_client import call_mcp

KIND = "link_lldp"
CEILING = 20
# Device ID, Local Intf, Hold-time, Capability, Port ID.
_ROW = re.compile(r"^(\S+)\s+(\S+)\s+(\d+)\s+(\S+)\s+(\S+)\s*$")
# A physical port, not a bundle, a MAC address or a port number.
_PORT = re.compile(r"[A-Za-z]+\d+(?:/\d+)+")
# IOS-XR cuts the Device ID to 20 characters.
_ID_WIDTH = 20


def parse_lldp(text: str) -> list[dict[str, str]] | None:
    """The neighbor table from ``show lldp neighbors``: [] when it lists none, None when not read."""
    text = (text or "").replace("\r", "")
    start = text.find("Device ID")
    if start < 0:
        return None
    rows: list[dict[str, str]] = []
    for line in text[start:].splitlines()[1:]:
        if line.startswith("Total entries"):
            break
        match = _ROW.match(line)
        if match:
            rows.append({"neighbor": match[1], "local": match[2], "port": match[5]})
    return rows


def _names(neighbor: str, device: str) -> bool:
    return (neighbor == device or neighbor.split(".", 1)[0] == device
            or (len(neighbor) == _ID_WIDTH and device.startswith(neighbor)))


def lldp_targets(case: CaseFile) -> list[str]:
    """The routers at the ends of each adjacency down, in issue order."""
    out: list[str] = []
    for issue in case.issues:
        if issue.get("code") not in _ADJACENCY_DOWN:
            continue
        for device, _ in _ends(str(issue.get("edge_id") or "")):
            if device not in out:
                out.append(device)
    return out


async def record_link_lldp(client: Any, case: CaseFile, *, ceiling: int = CEILING, workers: int = 1,
                           call: Any = None) -> None:
    """Read each end router's LLDP table once, within ``ceiling``, and keep it with the scan."""
    devices = lldp_targets(case)
    if not devices:
        return
    from diagnostic_mas.operational_checks.probe import Probe, safe
    from diagnostic_mas.operational_checks.scheduler import DeviceCalls, ProbeCache, run_groups

    chosen = [device for device in devices[:max(0, ceiling)] if safe(device)]
    calls, cache = DeviceCalls(call or call_mcp, max(1, workers)), ProbeCache()
    tables: dict[str, list[dict[str, str]]] = {}

    async def read(device: str) -> None:
        probe = Probe(client, calls, cache, max_calls=1)
        rows = parse_lldp(await probe.show(device, "lldp neighbors", required=False))
        if rows is not None:
            tables[device] = rows

    await run_groups(sorted(chosen), read, lambda device: device, max(1, workers))
    add_evidence(case, {"kind": KIND, "role": "physical", "layer": "physical", "payload": {
        "read_at": datetime.now(timezone.utc).isoformat(), "tables": dict(sorted(tables.items())),
        "wanted": len(devices), "left_out": max(0, len(devices) - max(0, ceiling)), "ceiling": ceiling}})


def _saved(case: CaseFile) -> dict[str, Any] | None:
    found = [ev.get("payload") for ev in case.evidence if ev.get("kind") == KIND]
    return found[-1] if found and isinstance(found[-1], dict) else None


def far_port(case: CaseFile, edge_id: str, device: str) -> str | None:
    """``device``'s port on this adjacency from the other end's LLDP table, when the adjacency names none."""
    from diagnostic_mas.port_counters import _parent_port

    ends = _ends(edge_id)
    tables = (_saved(case) or {}).get("tables") or {}
    for (end, interface), (other, other_interface) in zip(ends, reversed(ends)):
        if end != device or interface != "?" or other_interface == "?":
            continue
        local = _parent_port(other_interface)
        for row in tables.get(other) or []:
            if (_parent_port(row["local"]) == local and _names(row["neighbor"], device)
                    and _PORT.fullmatch(row["port"])):
                return _parent_port(row["port"])
    return None


def edge_ports(case: CaseFile, edge_id: str) -> list[tuple[str, str | None]]:
    """(device, physical port) at each end; a port the adjacency leaves unknown comes from LLDP or is None."""
    from diagnostic_mas.port_counters import _parent_port

    return [(device, _parent_port(interface) if interface != "?" else far_port(case, edge_id, device))
            for device, interface in _ends(edge_id)]


def _shown(neighbor: str, devices: list[str]) -> str:
    return next((device for device in devices if _names(neighbor, device)), neighbor)


def _direction(source: str, target: str, port: str | None, rows: list[dict[str, str]] | None,
               devices: list[str]) -> str:
    """``source`` → ``target``, as ``target``'s own table tells it."""
    from diagnostic_mas.port_counters import _parent_port

    head = f"{source} → {target}"
    if rows is None:
        return f"{head} not read on {target}"
    if not port:
        return f"{head}: {target}'s port not known"
    here = [row for row in rows if _parent_port(row["local"]) == port]
    if not here:
        return f"{head} not heard ({target} {port} lists no LLDP neighbor)"
    mine = [row for row in here if _names(row["neighbor"], source)]
    if mine:
        return f"{head} heard ({target} {port} lists {source} {mine[0]['port']})"
    return (f"{head} not heard ({target} {port} lists {_shown(here[0]['neighbor'], devices)} "
            f"{here[0]['port']} instead)")


def lldp_line(case: CaseFile, edge_id: str) -> str | None:
    """Each direction of an adjacency's link as the receiving end's LLDP tells it, or None when not read."""
    saved, ends = _saved(case), edge_ports(case, edge_id)
    if saved is None or not ends:
        return None
    tables = saved.get("tables") or {}
    (a, port_a), (b, port_b) = ends
    if a not in tables and b not in tables:
        return "LLDP not read."
    devices = sorted(getattr(case, "device_names", None) or [])
    return (f"LLDP: {_direction(a, b, port_b, tables.get(b), devices)}; "
            f"{_direction(b, a, port_a, tables.get(a), devices)}.")


def lldp_reads_line(case: CaseFile) -> str | None:
    """How many end routers this scan read, for the run details."""
    saved = _saved(case)
    if saved is None:
        return None
    left = int(saved.get("left_out") or 0)
    return (f"**LLDP reads:** {len(saved.get('tables') or {})} of {saved.get('wanted', 0)} routers at the ends "
            "of IS-IS adjacencies down"
            + (f"; {left} left out by the {saved.get('ceiling', CEILING)}-read limit" if left else ""))
