"""Port-mirror readiness: the device's mirror session, its sources and its ports.

The session is found by the service's destination interface in the device's own
answer; a session that is not listed is Unknown, never Down. The status layout
is the one Cisco documents for IOS-XR and was not yet sampled from a live device.
"""
import re
from nso_facts.l2vpn_xconnect import ac_name_from_endpoint
from nso_facts.topology.interfaces import interfaces_match
from .generic import check_interfaces
from .probe import safe

DIRECTIONS = {"both": "both", "rx-only": "rx", "tx-only": "tx"}


def sessions(text):
    """Sessions in ``show monitor-session status`` output; [] when not recognized."""
    found = []
    for block in re.split(r"^Monitor-session ", text, flags=re.M)[1:]:
        destination = re.search(r"^Destination interface (\S+)", block, re.M)
        if destination:
            rows = re.findall(r"^(\S+)[ \t]+(Rx|Tx|Both)[ \t]+(\S.*)$", block, re.M | re.I)
            found.append({"name": block.split(None, 1)[0], "destination": destination[1],
                          "sources": [(name, direction.lower(), status.strip()) for name, direction, status in rows]})
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
    text = await probe.show(device, "monitor-session status")
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
                probe.check("source", "fault", f"{source}: {row[2]}", device)
            elif row[1] != wanted:
                probe.check("source", "unknown", f"{source} is mirrored {row[1]}, intended {wanted}", device)
            else:
                probe.check("source", "pass", f"{source} {row[1]}: {row[2]}", device)
    await check_interfaces(probe, [(device, name) for name in (destination, *sources)])
