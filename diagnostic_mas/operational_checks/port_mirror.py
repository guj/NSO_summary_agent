"""Port-mirror readiness: the device's mirror session, its sources and its ports.

The session is found by the service's destination interface in the device's own
answer; a session that is not listed is Unknown, never Down. The layout read is
the one atla-data-sw returned for ``show monitor-session status detail`` on
13 October 2026: one block per session, one indented block per source.
"""
import re
from nso_facts.l2vpn_xconnect import ac_name_from_endpoint
from nso_facts.topology.interfaces import interfaces_match
from .generic import check_interfaces
from .probe import safe

DIRECTIONS = {"both": "both", "rx-only": "rx", "tx-only": "tx"}
STATUS = "monitor-session status detail"


def sessions(text):
    """Sessions in ``show monitor-session status detail`` output; [] when not recognized."""
    found = []
    for block in re.split(r"^Monitor-session ", (text or "").replace("\r", ""), flags=re.M)[1:]:
        destination = re.search(r"^[ \t]*Destination interface (\S+)", block, re.M)
        if destination:
            sources = []
            # A source is its name alone on a line, then its fields indented under it.
            for name, fields in re.findall(r"^[ \t]{2}(\S+)[ \t]*\n((?:[ \t]{4,}\S.*\n?)+)", block, re.M):
                direction = re.search(r"^\s*Direction:\s*(\S+)", fields, re.M)
                status = re.search(r"^\s*Status:\s*(\S.*)$", fields, re.M)
                if direction and status:
                    sources.append((name, direction[1].lower(), status[1].strip()))
            found.append({"name": block.split(None, 1)[0], "destination": destination[1], "sources": sources})
    return found


def _names(entries):
    entries = entries if isinstance(entries, list) else [entries]
    return [name for name in map(ac_name_from_endpoint, entries) if name and safe(name)]


async def check(instance, probe):
    device = instance.get("device")
    destinations = _names(instance.get("to-interface"))
    sources = _names((instance.get("from-interface") or []) + (instance.get("from-interface-vlan") or []))
    if not (isinstance(device, str) and safe(device) and len(destinations) == 1 and sources):
        probe.check("intent", "unknown", "Service does not name one device, one destination and a source", device)
        return
    destination = destinations[0]
    text = await probe.show(device, STATUS)
    session = next((s for s in sessions(text) if interfaces_match(destination, s["destination"])), None)
    if session is None:
        probe.check("session", "unknown", f"No mirror session to {destination} in the device's answer", device)
    else:
        probe.check("session", "pass", f"{session['name']} to {destination}", device)
        wanted = DIRECTIONS.get(str(instance.get("direction") or "both"))
        for source in sources:
            row = next((r for r in session["sources"] if interfaces_match(source, r[0])), None)
            if row is None:
                probe.check("source", "unknown", f"{source} is not listed under {session['name']}", device)
            elif not row[2].lower().startswith("operational"):
                # "Not operational (reason)" is the device's own verdict on its session: nothing is left to investigate.
                probe.check("source", "fault", f"{source}: {row[2]}", device,
                            conclusive=row[2].lower().startswith("not operational"))
            elif row[1] != wanted:
                probe.check("source", "unknown", f"{source} is mirrored {row[1]}, intended {wanted}", device)
            else:
                probe.check("source", "pass", f"{source} {row[1]}: {row[2]}", device)
    await check_interfaces(probe, [(device, name) for name in (destination, *sources)])
