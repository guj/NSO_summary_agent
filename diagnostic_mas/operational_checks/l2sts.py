"""EVPN bridge readiness using effective RTs and directional replication.

No explicit-config RT inference and no xconnect requirement. Only a complete
operational path can pass; absent MACs or unsupported views are not outages.
"""
import ipaddress
import re
from .bridge import collect
from .probe import cef_ready


def evpn(text, evi, bd):
    if not re.search(rf"^\s*{re.escape(evi)}\s+MPLS\s+{re.escape(bd)}\s+EVPN\s*$", text, re.M):
        return None
    imports, exports = set(), set()
    for rt, kind in re.findall(r"^\s*(\d+:\d+)\s+(Import|Export)\s*$", text, re.M):
        (imports if kind == "Import" else exports).add(rt)
    rd = re.search(r"RD Auto\s*:\s*(?:\(auto\) )?([0-9.]+):\d+", text)
    label = re.search(r"Multicast Label\s*:\s*(\d+)", text)
    try:
        peer = str(ipaddress.ip_address(rd[1])) if rd else None
    except ValueError:
        peer = None
    return {"imports": imports, "exports": exports, "peer": peer,
            "label": int(label[1]) if label else None}


def replication(text, evi, peer, label):
    chunks = re.split(rf"(?=^\s*{re.escape(evi)}\s+MPLS\s+)", text, flags=re.M)
    for block in chunks:
        if not re.match(rf"\s*{re.escape(evi)}\s+MPLS\s+\d+\s+{re.escape(peer)}\s*\n", block):
            continue
        if (re.search(r"Source\s*:\s*Remote", block) and re.search(r"PMSI Type\s*:\s*6\b", block)
                and re.search(rf"Nexthop\s*:\s*{re.escape(peer)}\s*(?:\n|$)", block)
                and re.search(rf"Label\s*:\s*{label}\s*(?:\n|$)", block)):
            return True
    return False


async def check(instance, probe):
    domains = await collect(instance, probe)
    if len(domains) < 2:
        probe.check("pe_scope", "unknown", "Both service PE identities are required")
    views = {}
    for device, domain in domains.items():
        evi, bd = domain["evi"], domain["identity"][1]
        if not evi:
            probe.check("evi_identity", "unknown", "No service EVI in scoped bridge evidence", device)
            continue
        text = await probe.show(device, f"evpn evi vpn-id {evi} detail")
        data = evpn(text, evi, bd)
        if not data or not data["imports"] or not data["exports"] or not data["peer"] or not data["label"]:
            probe.check("effective_evpn", "unknown", "Effective RTs, PE identity or label missing", device)
            continue
        data["replication"] = await probe.show(device, f"evpn evi vpn-id {evi} inclusive-multicast detail")
        data["evi"] = evi
        views[device] = data
        probe.check("effective_evpn", "pass", {k: sorted(v) if isinstance(v, set) else v for k,v in data.items() if k != "replication"}, device)
    for src, left in views.items():
        for dst, right in views.items():
            if src == dst:
                continue
            direction = f"{src} -> {dst}"
            # Import at source enables forwarding toward destination's advertised MACs.
            compatible = bool(left["imports"] & right["exports"])
            probe.check("rt_compatibility", "pass" if compatible else "fault", direction + ": destination export vs source import", src)
            replicated = replication(left["replication"], left["evi"], right["peer"], right["label"])
            probe.check("replication", "pass" if replicated else "unknown", direction + ": matching remote label and nexthop", src)
            if replicated:
                prefix = right["peer"] + "/32"
                cef = await probe.show(src, f"cef {prefix}")
                probe.check("transport", "pass" if cef_ready(cef, prefix, labeled=True) else "unknown", direction + ": resolved labeled transport", src)
    if len(views) != len(domains):
        probe.check("direction_coverage", "unknown", "Not all service PEs have usable EVPN evidence")
