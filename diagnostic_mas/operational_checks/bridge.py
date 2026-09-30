"""Shared bridge identity, membership and local forwarding checks."""
import re
from .probe import attachments, bridge_identity, interface_state, member_state
from nso_facts.topology.interfaces import interfaces_match
from nso_facts.topology.physical import parse_interfaces_brief


async def collect(instance, probe, *, routed=False):
    endpoints = attachments(instance)
    if not endpoints:
        probe.check("attachments", "unknown", "No supported attachment identities in service definition")
        return {}
    domains = {}
    for ep in endpoints:
        dev, ac = ep["device"], ep["ac"]
        # One run-local snapshot per device. Failed/ambiguous bulk discovery may
        # fall back to a scoped query; absent bulk rows never establish absence.
        brief = await probe.show(dev, "interfaces brief", required=False)
        matches = [v for k, v in parse_interfaces_brief(brief).items() if interfaces_match(ac, k)]
        if len(matches) == 1 and matches[0]["admin"] in {"up", "down", "admin-down"} and matches[0]["oper"] in {"up", "down"}:
            status = "pass" if matches[0]["admin"] == matches[0]["oper"] == "up" else "fault"
        else:
            iface = await probe.show(dev, f"interfaces {ac}")
            status = interface_state(iface, ac)
        probe.check("attachment", status, ac, dev)
        bulk = await probe.show(dev, "l2vpn bridge-domain detail", required=False)
        blocks = re.split(r"(?=Bridge group: )", bulk)
        selected = [b for b in blocks if bridge_identity(b) and member_state(b, ac) != "unknown"]
        if len(selected) > 1:
            probe.check("bridge_identity", "unknown", f"Ambiguous bridge membership for {ac}", dev)
            continue
        if len(selected) == 1:
            detail = selected[0]
            identity = bridge_identity(detail)
            lookup = bulk  # retains observed RP location for forwarding query
        else:
            lookup = await probe.show(dev, f"l2vpn bridge-domain interface {ac}")
            identity = bridge_identity(lookup)
            if not identity:
                probe.check("bridge_identity", "unknown", f"Exact bridge identity not established for {ac}", dev)
                continue
            bg, bd = identity
            detail = await probe.show(dev, f"l2vpn bridge-domain group {bg} detail")
            blocks = [b for b in re.split(r"(?=Bridge group: )", detail) if bridge_identity(b) == identity]
            detail = blocks[0] if len(blocks) == 1 else ""
        bg, bd = identity
        if dev in domains and domains[dev]["identity"] != identity:
            probe.check("membership", "unknown", "Service attachments span multiple bridge domains; requires investigation", dev)
        state = re.search(r"bridge-domain: [^,]+, id: \d+, state: (up|down)\b", detail, re.I)
        probe.check("bridge_state", "pass" if state and state[1].lower() == "up" else "fault" if state else "unknown", f"{bg}:{bd}", dev)
        probe.check("membership", member_state(detail, ac), f"{ac} in {bg}:{bd}", dev)
        members = re.split(r"(?=^\s+AC: )", detail, flags=re.M)
        members = [b for b in members if (m := re.match(r"\s+AC: ([^,]+),", b)) and interfaces_match(ac, m[1])]
        member = members[0] if len(members) == 1 else ""
        vlan = ep.get("vlan")
        encapsulation = False
        if str(vlan) == "0":
            encapsulation = bool(re.search(r"Type VLAN; Num Ranges: 0\b", member))
        elif vlan is not None and str(vlan).isdigit():
            encapsulation = bool(re.search(rf"VLAN ranges: \[{re.escape(str(vlan))}, {re.escape(str(vlan))}\]", member))
        if ep.get("inner_vlan") is not None:
            encapsulation = False  # QinQ semantics require the diagnostic path.
        probe.check("encapsulation", "pass" if encapsulation else "unknown", f"{ac} intended VLAN {vlan}", dev)
        loc = re.search(r"(?:^|\n)([A-Za-z]+/\d+/RP\d+/CPU\d+):", lookup)
        forwarding = ""
        if loc:
            location = loc[1].split("/", 1)[1]
            forwarding = await probe.show(dev, f"l2vpn forwarding bridge-domain {bg}:{bd} detail location {location}")
        valid = bool(re.search(rf"Bridge-domain name: {re.escape(bg)}:{re.escape(bd)}, id: \d+, state: up\b", forwarding))
        probe.check("local_forwarding", member_state(forwarding, ac, True) if valid else "unknown", f"Programmed bridge member {ac}", dev)
        # Zero MACs can be healthy only with a demonstrated local flood path.
        flood = bool(re.search(r"Broadcast & Multicast: enabled", forwarding) and re.search(r"Unknown unicast: enabled", forwarding))
        if not routed:
            probe.check("local_flooding", "pass" if valid and flood else "unknown", f"Flooding in {bg}:{bd}", dev)
        if not routed and re.search(r"Split Horizon Group: (?!none)[^\n]+|E-Tree: Leaf", detail, re.I):
            probe.check("switching_policy", "unknown", "Non-default switching restrictions require investigation", dev)
        evpn_state = re.search(r"EVPN, state: (up|down)\b", detail, re.I)
        if evpn_state:
            probe.check("evpn_binding", "pass" if evpn_state[1].lower() == "up" else "fault", f"{bg}:{bd}", dev)
        evi = re.search(r"EVI: (\d+)(?:[^\n]*state[^\n]*up)?", detail, re.I)
        domains[dev] = {"identity": identity, "detail": detail, "evi": evi[1] if evi else None}
    return domains
