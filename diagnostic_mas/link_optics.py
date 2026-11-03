"""Optics at both ends of an IS-IS adjacency that is down at a router that answered.

Read once per scan, after the spines: each end's transmit and receive power per lane and
its DOM alarms, from IOS-XR ``show controllers <port>``. The report sets one direction
against the other in numbers only; it never names a cause. When LLDP shows another device
on the link, one end's transmit is not the other's receive, so each end is given on its
own. A read that fails, or a platform without that output, is "not read".

An end that is a bundle has no optics of its own: its members and their state are read from
``show bundle``, each end on its own (members cannot be matched across the link). Their
optics are left to the investigation, which then knows them by name.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from diagnostic_mas.case import CaseFile, add_evidence
from nso_facts.mcp_client import call_mcp
from nso_facts.topology.interfaces import interface_variants

KIND = "link_optics"
CEILING = 20
_ADJACENCY_DOWN = frozenset({"configured_no_adjacency", "unidirectional_adjacency"})
# One row of the DOM table: lane, wavelength, Tx dBm, Tx mW, Rx dBm, Rx mW, laser bias.
_LANE = re.compile(r"^\s*(\d+)\s+(?:n/a|[\d.]+)\s+(-?\d+(?:\.\d+)?)\s+[\d.]+\s+(-?\d+(?:\.\d+)?)\s+[\d.]+\s+[\d.]+\s*$")
_STAMP = re.compile(r"^\s*(\w{3} \w{3} +\d+ [\d:.]+ \w+)\s*$", re.M)
_BUNDLE_PORT = re.compile(r"Bundle-Ether\d+")
_LINKS = re.compile(r"Local links <active/standby/configured>:\s*(\d+)\s*/\s*\d+\s*/\s*(\d+)")


def parse_optics(text: str) -> dict[str, Any] | None:
    """Per-lane transmit and receive power and the DOM alarms, from ``show controllers <port>``."""
    text = (text or "").replace("\r", "")
    start = text.find("Digital Optical Monitoring")
    if start < 0:
        return None
    end = text.find("DOM alarms:", start)
    table = text[start:end if end > 0 else len(text)]
    lanes = [{"lane": int(m[1]), "tx_dbm": float(m[2]), "rx_dbm": float(m[3])}
             for m in (_LANE.match(line) for line in table.splitlines()) if m]
    if not lanes:
        return None
    alarms: list[str] = []
    for line in text[end + len("DOM alarms:"):].splitlines() if end > 0 else []:
        line = line.strip()
        if line.startswith(("Alarm ", "Thresholds")):
            break
        if line:
            alarms.append(line)
    stamp = _STAMP.search(text)
    return {"lanes": lanes, "alarms": ", ".join(alarms), "read_at": stamp[1] if stamp else None}


def parse_bundle(text: str) -> dict[str, Any] | None:
    """A bundle's active and configured member counts and each member's state, from ``show bundle <name>``."""
    text = (text or "").replace("\r", "")
    links, table = _LINKS.search(text), text.find("\n  ----")
    if not links or table < 0:
        return None
    members: list[dict[str, str]] = []
    for line in text[table:].splitlines()[2:]:
        columns = re.split(r"\s{2,}", line.strip())
        if len(line) - len(line.lstrip()) <= 2 and len(columns) >= 3:
            port = max(interface_variants(columns[0]) or {columns[0]}, key=len)
            members.append({"port": port, "state": columns[2], "note": ""})
        elif members and line.startswith("   ") and line.strip():
            # The line under a member, such as "Link is down".
            members[-1]["note"] = " ".join(filter(None, (members[-1]["note"], line.strip())))
    return {"active": int(links[1]), "configured": int(links[2]), "members": members}


def _ends(edge_id: str) -> list[tuple[str, str]]:
    parts = edge_id.split(":")
    return [(parts[1], parts[2]), (parts[3], parts[4])] if len(parts) == 5 and parts[0] == "isis" else []


def optics_targets(case: CaseFile) -> list[tuple[str, str]]:
    """(device, port) at each end of an adjacency down whose port is known, in issue order.

    A port the adjacency leaves unknown counts when the other end's LLDP named it.
    """
    from diagnostic_mas.link_lldp import edge_ports

    out: list[tuple[str, str]] = []
    for issue in case.issues:
        if issue.get("code") not in _ADJACENCY_DOWN:
            continue
        for device, port in edge_ports(case, str(issue.get("edge_id") or "")):
            if port and (device, port) not in out:
                out.append((device, port))
    return out


async def record_link_optics(client: Any, case: CaseFile, *, ceiling: int = CEILING, workers: int = 1,
                             call: Any = None) -> None:
    """Read each end's optics once, within ``ceiling``, and keep the readings with the scan.

    A bundle end is read as its member list instead, one ``show bundle`` read. Every read counts
    against ``ceiling``, the bundle reads first.
    """
    targets = optics_targets(case)
    if not targets:
        return
    from diagnostic_mas.operational_checks.probe import Probe, safe
    from diagnostic_mas.operational_checks.scheduler import DeviceCalls, ProbeCache, run_groups

    calls, cache = DeviceCalls(call or call_mcp, max(1, workers)), ProbeCache()
    budget = max(0, ceiling)

    async def read_each(wanted: list[tuple[str, str]], command: str, parse: Any, into: dict[str, Any]) -> None:
        by_device: dict[str, list[str]] = {}
        for device, port in wanted:
            if safe(device) and safe(port):
                by_device.setdefault(device, []).append(port)

        async def read(device: str) -> None:
            probe = Probe(client, calls, cache, max_calls=len(by_device[device]))
            for port in by_device[device]:
                into[f"{device}:{port}"] = parse(await probe.show(device, f"{command} {port}", required=False))

        await run_groups(sorted(by_device), read, lambda device: device, max(1, workers))

    bundle_ends = [(device, port) for device, port in targets if _BUNDLE_PORT.fullmatch(port)][:budget]
    bundles: dict[str, dict[str, Any] | None] = {}
    await read_each(bundle_ends, "bundle", parse_bundle, bundles)
    budget -= len(bundle_ends)
    wanted = [(device, port) for device, port in targets if not _BUNDLE_PORT.fullmatch(port)]
    found: dict[str, dict[str, Any] | None] = {}
    await read_each(wanted[:budget], "controllers", parse_optics, found)
    ports = {key: reading for key, reading in found.items() if reading}
    add_evidence(case, {"kind": KIND, "role": "physical", "layer": "physical", "payload": {
        "read_at": datetime.now(timezone.utc).isoformat(), "ports": dict(sorted(ports.items())),
        **({"bundles": dict(sorted(bundles.items()))} if bundles else {}),
        "wanted": len(wanted), "left_out": max(0, len(wanted) - budget), "ceiling": ceiling}})


def _saved(case: CaseFile) -> dict[str, Any] | None:
    found = [ev.get("payload") for ev in case.evidence if ev.get("kind") == KIND]
    return found[-1] if found and isinstance(found[-1], dict) else None


def _span(values: list[float], fmt: str) -> str:
    low, high = format(min(values), fmt), format(max(values), fmt)
    return low if low == high else f"{low} to {high}"


def _direction(source: str, target: str, sent: dict[str, Any] | None, got: dict[str, Any] | None) -> str:
    bits = [f"transmitted {_span([lane['tx_dbm'] for lane in sent['lanes']], '+.1f')} dBm" if sent
            else f"not read on {source}",
            f"received {_span([lane['rx_dbm'] for lane in got['lanes']], '+.1f')} dBm" if got
            else f"not read on {target}"]
    if sent and got:
        # Lane n at one end is lane n (the same wavelength) at the other.
        received = {lane["lane"]: lane["rx_dbm"] for lane in got["lanes"]}
        loss = [lane["tx_dbm"] - received[lane["lane"]] for lane in sent["lanes"] if lane["lane"] in received]
        if loss:
            bits.append(f"loss {_span(loss, '.1f')} dB per lane")
    return f"{source} → {target}: " + ", ".join(bits)


def _powers(reading: dict[str, Any] | None) -> str:
    if not reading:
        return "not read"
    return (f"transmitted {_span([lane['tx_dbm'] for lane in reading['lanes']], '+.1f')} dBm, "
            f"received {_span([lane['rx_dbm'] for lane in reading['lanes']], '+.1f')} dBm")


def _alone(device: str, reading: dict[str, Any] | None) -> str:
    return f"{device} {_powers(reading)}"


def _member(member: dict[str, str]) -> str:
    """A member as ``show bundle`` gives it: its state, and the line under it when there is one."""
    said = ", ".join(filter(None, (member.get("state"), member.get("note"))))
    return f"{member['port']} ({said})" if said else member["port"]


def _bundle_ends(saved: dict[str, Any], ends: list[tuple[str, str | None]]) -> str:
    """Each end on its own, a bundle as its members and their state: they cannot be matched across the link."""
    ports, bundles = saved.get("ports") or {}, saved.get("bundles") or {}
    parts = []
    for device, port in ends:
        if port and _BUNDLE_PORT.fullmatch(port):
            info = bundles.get(f"{device}:{port}")
            parts.append(f"{device} {port}: members not read" if not info else
                         f"{device} {port}, {info['active']} of {info['configured']} members active: "
                         + ", ".join(_member(member) for member in info["members"]))
        else:
            reading = ports.get(f"{device}:{port}") if port else None
            alarms = reading.get("alarms") if reading else None
            parts.append(_alone(device, reading)
                         + (f" (DOM alarms: {alarms})" if alarms and alarms.lower() != "no alarms" else ""))
    return "Bundle members, each end on its own: " + "; ".join(parts) + ". Member optics not read."


def optics_line(case: CaseFile, edge_id: str) -> str | None:
    """Both directions of an adjacency's link in numbers, or None when this scan read no optics.

    With another device on the link, each end's own readings, not set against the other's;
    when an end is a bundle, each end's members and their state instead.
    """
    from diagnostic_mas.link_lldp import ANOTHER_DEVICE, edge_ports, hears_another_device

    saved, ends = _saved(case), edge_ports(case, edge_id)
    if saved is None or not ends:
        return None
    if any(port and _BUNDLE_PORT.fullmatch(port) for _, port in ends):
        return _bundle_ends(saved, ends)
    ports = saved.get("ports") or {}
    (a, _), (b, _) = ends
    read = {device: ports.get(f"{device}:{port}") if port else None for device, port in ends}
    if not read[a] and not read[b]:
        return "Optics not read."
    alarms = [f"{device}: {reading['alarms']}" for device, reading in read.items()
              if reading and reading.get("alarms") and reading["alarms"].lower() != "no alarms"]
    said = ("DOM alarms on " + "; on ".join(alarms) + ".") if alarms else "No DOM alarms."
    if hears_another_device(case, edge_id):
        return f"Optics, each end on its own ({ANOTHER_DEVICE}): {_alone(a, read[a])}; {_alone(b, read[b])}. {said}"
    return (f"Optics: {_direction(a, b, read[a], read[b])}; {_direction(b, a, read[b], read[a])}. {said}")


def optics_reads_line(case: CaseFile) -> str | None:
    """How many ends this scan read, for the run details."""
    saved = _saved(case)
    if saved is None:
        return None
    left = int(saved.get("left_out") or 0)
    bundles = sum(1 for info in (saved.get("bundles") or {}).values() if info)
    return (f"**Optics reads:** {len(saved.get('ports') or {})} of {saved.get('wanted', 0)} ports at the ends "
            "of IS-IS adjacencies down"
            + (f"; {bundles} bundle{'s' * (bundles != 1)} read for their members" if bundles else "")
            + (f"; {left} left out by the {saved.get('ceiling', CEILING)}-read limit" if left else ""))
