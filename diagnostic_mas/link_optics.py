"""Optics at both ends of an IS-IS adjacency that is down at a router that answered, and of a link that is up but
gaining errors.

Read once per scan, after the spines: each end's transmit and receive power per lane and
its DOM alarms, from IOS-XR ``show controllers <port>``. The report sets one direction
against the other in numbers only; it never names a cause. When LLDP shows another device
on the link, one end's transmit is not the other's receive, so each end is given on its
own. A read that fails, or a platform without that output, is "not read".

A breakout lane (``HundredGigE0/0/0/64/3``) is read from its parent port's ``show controllers optics 0/0/0/64``:
one read for all the lanes, each lane keeping its own part.

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
# Reads for the sweep of the other ports that are up; a ceiling of its own, so it never takes reads from the ports above.
SWEEP_CEILING = 400
# Raw replies kept for a read that gave no table or an alarm, so a layout never seen before can be read from the text:
# up to this many of each kind, each a window of this many characters around the optics table.
SAMPLES = 3
SAMPLE_CHARS = 2500
_SAMPLE_MARKERS = ("Digital Optical Monitoring", "Lane  Laser Bias", "Detected Alarms", "DOM alarm")
_ADJACENCY_DOWN = frozenset({"configured_no_adjacency", "unidirectional_adjacency"})
# One row of the DOM table: lane, wavelength, Tx dBm, Tx mW, Rx dBm, Rx mW, laser bias. A value the device flags has the
# flag letter glued to it (``2.5485H`` is a receive power in alarm high, ``h`` is warning high, ``L`` and ``l`` low).
_FLAG = r"[HhLl]?"
_NUM = rf"-?\d+(?:\.\d+)?{_FLAG}"
_LANE = re.compile(rf"^\s*(\d+)\s+(?:n/a|[\d.]+)\s+(-?\d+(?:\.\d+)?){_FLAG}\s+{_NUM}\s+"
                   rf"(-?\d+(?:\.\d+)?){_FLAG}\s+{_NUM}\s+{_NUM}\s*$")
_STAMP = re.compile(r"^\s*(\w{3} \w{3} +\d+ [\d:.]+ \w+)\s*$", re.M)
_BUNDLE_PORT = re.compile(r"Bundle-Ether\d+")
_BREAKOUT_LANE = re.compile(r"(\d+/\d+/\d+/\d+)/(\d+)$")
# One row of the lane table of ``show controllers optics <port>``: lane, laser bias, TX dBm, RX dBm, output frequency.
_PARENT_LANE = re.compile(r"^\s*(\d+)\s+-?[\d.]+\s*mA\s+(-?\d+(?:\.\d+)?)\s*dBm\s+(-?\d+(?:\.\d+)?)\s*dBm\b")
# High alarm, low alarm, high warning, low warning.
_RX_THRESHOLD = re.compile(r"Rx Power Threshold\(dBm\)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)")
# The threshold table of ``show controllers <port>``: alarm high, warning high, warning low, alarm low.
_PLAIN_RX_THRESHOLD = re.compile(r"Receive Power \(dBm\):\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)")
_SINGLE_TX = re.compile(r"Actual TX Power\s*=\s*(-?\d+(?:\.\d+)?)\s*dBm")
_SINGLE_RX = re.compile(r"\bRX Power\s*=\s*(-?\d+(?:\.\d+)?)\s*dBm")
_LANE_ALARM = re.compile(r"(?:LOW|HIGH)-(?:RX|TX)(\d+)-[A-Z]+")
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
    stamp, limits = _STAMP.search(text), _PLAIN_RX_THRESHOLD.search(text)
    return {"lanes": lanes, "alarms": ", ".join(alarms), "read_at": stamp[1] if stamp else None,
            "rx_high_alarm_dbm": float(limits[1]) if limits else None,
            "rx_low_alarm_dbm": float(limits[4]) if limits else None}


def parse_parent_optics(text: str) -> dict[str, Any] | None:
    """Per-lane power, the receive alarm thresholds and the alarms, from ``show controllers optics <port>``.

    This is the read for a breakout lane: the parent port's optics carry every lane, and lane n is
    the n-th breakout (``HundredGigE0/0/0/64/3`` is lane 3 of ``0/0/0/64``).
    """
    text = (text or "").replace("\r", "")
    start = text.find("Lane  Laser Bias")
    if start >= 0:
        lanes = [{"lane": int(m[1]), "tx_dbm": float(m[2]), "rx_dbm": float(m[3])}
                 for m in (_PARENT_LANE.match(line) for line in text[start:].splitlines()) if m]
    else:
        # A single optic (an SFP+ in a breakout cage) has no lane table: its one reading is lane 0.
        tx, rx = _SINGLE_TX.search(text), _SINGLE_RX.search(text)
        lanes = [{"lane": 0, "tx_dbm": float(tx[1]), "rx_dbm": float(rx[1])}] if tx and rx else []
    if not lanes:
        return None
    alarms: list[str] = []
    found = text.find("Detected Alarms:")
    if found >= 0:
        # The alarm text is on the next lines, or on this one when the device names it there ("None" is no alarm).
        after = text[found + len("Detected Alarms:"):].splitlines()
        if after and after[0].strip().lower() not in ("", "none"):
            alarms += after[0].split()
        for line in after[1:]:
            if not line.strip() or ":" in line:
                break
            alarms += line.split()
    threshold, stamp = _RX_THRESHOLD.search(text), _STAMP.search(text)
    return {"lanes": lanes, "alarms": " ".join(alarms), "read_at": stamp[1] if stamp else None,
            "rx_low_alarm_dbm": float(threshold[2]) if threshold else None,
            "rx_low_warning_dbm": float(threshold[4]) if threshold else None}


def _lane_reading(parent: dict[str, Any], lane: int, name: str) -> dict[str, Any] | None:
    """One breakout lane's part of its parent's reading; only the alarms that name this lane (RX1 is lane 0)."""
    mine = [row for row in parent["lanes"] if row["lane"] == lane]
    if not mine:
        return None
    alarms = [token for token in parent["alarms"].split()
              if (match := _LANE_ALARM.fullmatch(token)) and int(match[1]) == lane + 1]
    return {"lanes": mine, "alarms": " ".join(alarms), "read_at": parent["read_at"], "parent": name, "lane": lane,
            "rx_low_alarm_dbm": parent["rx_low_alarm_dbm"], "rx_low_warning_dbm": parent["rx_low_warning_dbm"]}


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


def gaining_error_ends(case: CaseFile) -> list[tuple[str, str]]:
    """(device, port) of each port that is up and gained errors since the previous scan, and of its far end.

    A port that gained only output drops is left out.

    Worked out from this scan's counters and the previous scan's, which the scan already holds; a port
    on its own is not enough to tell a dirty connector from a failing optic, but the light at both ends is.
    """
    from diagnostic_mas.port_counters import _parent_port, links_gaining_errors

    previous = getattr(case, "previous_port_counters", None)
    if not previous:
        return []
    rows = links_gaining_errors({"evidence": [{"kind": "port_counters", "payload": previous}]},
                                {"evidence": case.evidence})
    out: list[tuple[str, str]] = []
    for row in rows:
        # Output drops alone are a congestion or policy count: the light at the ends says nothing about them.
        if set(row["gained"]) <= {"output_drops"} and not row["state_changes"] and not row.get("bounced"):
            continue
        ends = [(str(row["device"]), str(row["port"]))]
        far = row.get("far_end") or {}
        if far.get("interface") not in (None, "?"):
            ends.append((str(far["device"]), _parent_port(str(far["interface"]))))
        out += [end for end in ends if end not in out]
    return out


def sweep_ends(case: CaseFile, taken: set[str]) -> list[tuple[str, str]]:
    """(device, port) of each physical port this scan read counters on that is up, bar a bundle and a port in ``taken``."""
    found = [ev.get("payload") for ev in case.evidence if ev.get("kind") == "port_counters"]
    ports = (found[-1] or {}).get("ports") if found and isinstance(found[-1], dict) else None
    out = []
    for key, reading in sorted((ports or {}).items()):
        device, _, port = key.partition(":")
        if (isinstance(reading, dict) and reading.get("state") == "up" and port and key not in taken
                and not _BUNDLE_PORT.fullmatch(port)):
            out.append((device, port))
    return out


def _excerpt(text: str) -> str:
    """A window of the reply around its optics table (else its start), at most ``SAMPLE_CHARS`` long."""
    text = (text or "").replace("\r", "")
    found = [text.find(marker) for marker in _SAMPLE_MARKERS if marker in text]
    start = max(0, min(found) - 300) if found else 0
    return text[start:start + SAMPLE_CHARS]


class _Reads:
    """One scan's optics reads: the client, the call path with its cache, and the device concurrency."""

    def __init__(self, client: Any, call: Any, workers: int) -> None:
        from diagnostic_mas.operational_checks.scheduler import DeviceCalls, ProbeCache

        self.client, self.workers = client, max(1, workers)
        self.calls, self.cache = DeviceCalls(call or call_mcp, self.workers), ProbeCache()
        self.samples: list[dict[str, str]] = []

    def keep(self, key: str, command: str, text: str, reading: dict[str, Any] | None) -> None:
        """Keep the raw reply of a read that gave no table, or whose device reported an alarm."""
        if not (text or "").strip():
            return          # an error or an empty answer has no layout to read
        said = ((reading or {}).get("alarms") or "").strip().lower()
        why = "no table" if reading is None else "alarm" if said not in ("", "no alarms") else None
        if why:
            self.samples.append({"port": key, "command": command, "why": why, "text": _excerpt(text)})

    def chosen(self) -> list[dict[str, str]]:
        """Up to ``SAMPLES`` of each kind in port order; one reply from a device that answers every port alike."""
        out: list[dict[str, str]] = []
        for why in ("no table", "alarm"):
            seen: set[tuple[str, str]] = set()
            for sample in sorted((s for s in self.samples if s["why"] == why), key=lambda s: s["port"]):
                same = (sample["port"].partition(":")[0], sample["text"][:80].strip())
                if why == "no table" and same in seen:
                    continue
                seen.add(same)
                if sum(1 for kept in out if kept["why"] == why) < SAMPLES:
                    out.append(sample)
        return out

    async def each(self, wanted: list[tuple[str, str]], command: str, parse: Any, into: dict[str, Any],
                   *, keep: bool = False) -> None:
        from diagnostic_mas.operational_checks.probe import Probe, safe
        from diagnostic_mas.operational_checks.scheduler import run_groups

        by_device: dict[str, list[str]] = {}
        for device, port in wanted:
            if safe(device) and safe(port):
                by_device.setdefault(device, []).append(port)

        async def read(device: str) -> None:
            probe = Probe(self.client, self.calls, self.cache, max_calls=len(by_device[device]))
            for port in by_device[device]:
                text = await probe.show(device, f"{command} {port}", required=False)
                into[f"{device}:{port}"] = reading = parse(text)
                if keep:
                    self.keep(f"{device}:{port}", f"{command} {port}", text, reading)

        await run_groups(sorted(by_device), read, lambda device: device, self.workers)

    async def ports(self, wanted: list[tuple[str, str]], budget: int, found: dict[str, Any]) -> tuple[list[str], int]:
        """Read ports (none a bundle) within ``budget`` reads into ``found``: the keys asked for and the count left out."""
        from diagnostic_mas.operational_checks.probe import Probe, safe
        from diagnostic_mas.operational_checks.scheduler import run_groups

        plain = [end for end in wanted if not _BREAKOUT_LANE.search(end[1])]
        plain_read = plain[:max(0, budget)]
        await self.each(plain_read, "controllers", parse_optics, found, keep=True)
        # A breakout lane shares its parent port's optics: one read per parent, each lane keeping its own part.
        by_parent: dict[tuple[str, str], list[tuple[str, int]]] = {}
        for device, port in wanted:
            if (lane := _BREAKOUT_LANE.search(port)) and safe(device) and safe(port):
                by_parent.setdefault((device, lane[1]), []).append((port, int(lane[2])))
        parents = list(by_parent)[:max(0, budget - len(plain_read))]

        async def read_parents(device: str) -> None:
            probe = Probe(self.client, self.calls, self.cache, max_calls=len(parents))
            for owner, parent in (key for key in parents if key[0] == device):
                text = await probe.show(device, f"controllers optics {parent}", required=False)
                reading = parse_parent_optics(text)
                self.keep(f"{device}:{parent}", f"controllers optics {parent}", text, reading)
                for port, lane in by_parent[owner, parent]:
                    found[f"{device}:{port}"] = _lane_reading(reading, lane, parent) if reading else None

        await run_groups(sorted({device for device, _ in parents}), read_parents, lambda device: device, self.workers)
        asked = ([f"{device}:{port}" for device, port in plain_read if safe(device) and safe(port)]
                 + [f"{owner[0]}:{port}" for owner in parents for port, _ in by_parent[owner]])
        left = (len(plain) - len(plain_read)) + sum(len(lanes) for key, lanes in by_parent.items() if key not in parents)
        return asked, left


async def record_link_optics(client: Any, case: CaseFile, *, ceiling: int = CEILING, workers: int = 1,
                             call: Any = None) -> None:
    """Read each end's optics once, within ``ceiling``, and keep the readings with the scan.

    A bundle end is read as its member list instead, one ``show bundle`` read. Every read counts
    against ``ceiling``, the bundle reads first. The other ports that are up are then read within
    ``SWEEP_CEILING``, so a transceiver in alarm shows before its link has any other sign.
    """
    targets = optics_targets(case)
    # After the adjacencies down, so they keep their place under the ceiling.
    extra = [end for end in gaining_error_ends(case) if end not in targets]
    targets = targets + extra
    sweep = sweep_ends(case, {f"{device}:{port}" for device, port in targets})
    if not targets and not sweep:
        return
    reads, budget = _Reads(client, call, workers), max(0, ceiling)
    bundle_ends = [(device, port) for device, port in targets if _BUNDLE_PORT.fullmatch(port)][:budget]
    bundles: dict[str, dict[str, Any] | None] = {}
    await reads.each(bundle_ends, "bundle", parse_bundle, bundles)
    wanted = [(device, port) for device, port in targets if not _BUNDLE_PORT.fullmatch(port)]
    found: dict[str, dict[str, Any] | None] = {}
    _, left_out = await reads.ports(wanted, budget - len(bundle_ends), found)
    swept, sweep_left_out = await reads.ports(sweep, SWEEP_CEILING, found)
    ports = {key: reading for key, reading in found.items() if reading}
    add_evidence(case, {"kind": KIND, "role": "physical", "layer": "physical", "payload": {
        "read_at": datetime.now(timezone.utc).isoformat(), "ports": dict(sorted(ports.items())),
        **({"bundles": dict(sorted(bundles.items()))} if bundles else {}),
        "wanted": len(wanted), "left_out": left_out, "ceiling": ceiling,
        "also_gaining": len(extra), "swept": sorted(swept), "sweep_left_out": sweep_left_out,
        **({"raw_samples": reads.chosen()} if reads.samples else {})}})


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


def _latest_optics(evidence: list[Any]) -> dict[str, Any] | None:
    found = [ev.get("payload") for ev in evidence or [] if isinstance(ev, dict) and ev.get("kind") == KIND]
    return found[-1] if found and isinstance(found[-1], dict) else None


def _end(device: str, port: str | None, reading: dict[str, Any] | None) -> str:
    """One end's light; a breakout lane names its port and parent and, when it is in range, the alarm threshold."""
    if not reading or not reading.get("parent"):
        return _alone(device, reading)
    low = reading.get("rx_low_alarm_dbm")
    return (f"{device} {port} (lane {reading['lane']} of {reading['parent']}) {_powers(reading)}"
            + (f" (receive low alarm {low:+.1f} dBm)" if low is not None else ""))


def _alarm_findings(read: dict[str, dict[str, Any] | None], port_of: dict[str, str | None]) -> list[dict[str, Any]]:
    """The alarms a device itself reported at one end of a link, with the other end's transmit for company."""
    out = []
    for device, reading in read.items():
        said = (reading or {}).get("alarms") or ""
        if not reading or not said or said.lower() == "no alarms":
            continue
        rx = [lane["rx_dbm"] for lane in reading["lanes"]]
        other = next(((name, got) for name, got in read.items() if name != device and got), None)
        out.append({
            "device": device, "port": port_of[device], "alarms": said,
            "rx_dbm": min(rx) if rx else None, "rx_low_alarm_dbm": reading.get("rx_low_alarm_dbm"),
            **({"parent": reading["parent"], "lane": reading["lane"]} if reading.get("parent") else {}),
            "other": ({"device": other[0], "tx": _span([lane["tx_dbm"] for lane in other[1]["lanes"]], "+.1f")}
                      if other else None)})
    return out


def with_optics(rows: list[dict[str, Any]], case: dict[str, Any]) -> list[dict[str, Any]]:
    """Give each link-gaining-errors row the light read at its two ends, when this scan read any.

    Each end is given on its own, with no loss figure between them: the report does not name a cause here.
    """
    from diagnostic_mas.port_counters import _parent_port

    saved = _latest_optics(case.get("evidence"))
    if saved is None:
        return rows
    ports, bundles = saved.get("ports") or {}, saved.get("bundles") or {}
    for row in rows:
        ends: list[tuple[str, str | None]] = [(str(row["device"]), str(row["port"]))]
        far = row.get("far_end") or {}
        if far.get("interface") not in (None, "?"):
            ends.append((str(far["device"]), _parent_port(str(far["interface"]))))
        if any(f"{device}:{port}" in bundles for device, port in ends if port):
            row["optics"] = _bundle_ends(saved, ends)
            continue
        read = {device: ports.get(f"{device}:{port}") for device, port in ends if port}
        port_of = {device: port for device, port in ends}
        if not any(read.values()):
            continue
        alarms = [f"{device}: {reading['alarms']}" for device, reading in read.items()
                  if reading and reading.get("alarms") and reading["alarms"].lower() != "no alarms"]
        found = _alarm_findings(read, port_of)
        if found:
            row["optics_alarms"] = found
        row["optics"] = ("Optics, each end on its own: " + "; ".join(_end(device, port_of[device], reading)
                                                                     for device, reading in read.items())
                         + ". " + (("DOM alarms on " + "; on ".join(alarms) + ".") if alarms else "No DOM alarms."))
    return rows


def optics_alarm_leads(case: CaseFile) -> list[dict[str, Any]]:
    """Swept ports (up, not otherwise read) whose own device reports an optics alarm: a lead, never a status.

    A breakout lane carries only the alarms that name it. Ports read for a down adjacency or a link gaining
    errors are not here: those reports already carry their alarm.
    """
    saved = _saved(case) or {}
    ports = saved.get("ports") or {}
    out = []
    for key in saved.get("swept") or []:
        reading = ports.get(key)
        said = (reading or {}).get("alarms") or ""
        if not said or said.lower() == "no alarms":
            continue
        device, _, port = key.partition(":")
        out.append({"device": device, "port": port, "alarms": said,
                    **({"parent": reading["parent"], "lane": reading["lane"]} if reading.get("parent") else {}),
                    **_beyond_threshold(reading, said)})
    return out


def _beyond_threshold(reading: dict[str, Any], said: str) -> dict[str, Any]:
    """The worst lane's receive level against the device's own alarm threshold, when the device's words name a side.

    The side comes from the alarm text ("Alarm high", "LOW-RX1-PWR") and the threshold from the same reply, so no
    number here is the agent's; nothing is returned when the level is not beyond the threshold.
    """
    low, high = "low" in said.lower(), "high" in said.lower()
    if low == high or not reading.get("lanes"):
        return {}
    side = "high" if high else "low"
    limit = reading.get("rx_high_alarm_dbm" if high else "rx_low_alarm_dbm")
    worst = (max if high else min)(reading["lanes"], key=lambda lane: lane["rx_dbm"])
    if limit is None or not (worst["rx_dbm"] > limit if high else worst["rx_dbm"] < limit):
        return {}
    return {"rx": {"side": side, "dbm": worst["rx_dbm"], "lane": worst["lane"], "threshold_dbm": limit}}


def optics_sweep_counts(case: CaseFile) -> dict[str, int] | None:
    """Ports with an alarm of their own and ports read in the sweep; None when the scan did not sweep."""
    saved = _saved(case) or {}
    if not (saved.get("swept") or saved.get("sweep_left_out")):
        return None
    ports = saved.get("ports") or {}
    return {"alarm_ports": len(optics_alarm_leads(case)), "ports_read": sum(1 for k in saved["swept"] if ports.get(k))}


def optics_reads_line(case: CaseFile) -> str | None:
    """How many ends this scan read, and how many ports of the sweep, for the run details."""
    saved = _saved(case)
    if saved is None:
        return None
    left = int(saved.get("left_out") or 0)
    bundles = sum(1 for info in (saved.get("bundles") or {}).values() if info)
    ports, swept = saved.get("ports") or {}, saved.get("swept") or []
    ends = ""
    if saved.get("wanted") or saved.get("bundles"):
        ends = (f"{sum(1 for key in ports if key not in swept)} of {saved.get('wanted', 0)} ports at the ends "
                "of IS-IS adjacencies down"
                + (" and of links gaining errors" if saved.get("also_gaining") else "")
                + (f"; {bundles} bundle{'s' * (bundles != 1)} read for their members" if bundles else "")
                + (f"; {left} left out by the {saved.get('ceiling', CEILING)}-read limit" if left else ""))
    sweep_left = int(saved.get("sweep_left_out") or 0)
    sweep = ""
    if swept or sweep_left:
        read = sum(1 for key in swept if ports.get(key))
        sweep = (f"sweep of ports on links that are up: {read} of {len(swept) + sweep_left} read, "
                 f"{len(optics_alarm_leads(case))} with an alarm"
                 + (f"; {sweep_left} left out by the {SWEEP_CEILING}-read limit" if sweep_left else ""))
    return "**Optics reads:** " + "; ".join(part for part in (ends, sweep) if part)
