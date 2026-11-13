"""Shared bridge identity, membership and local forwarding checks."""
import re
from .probe import (attachments, bridge_identity, bridge_view, first_line, focus, interface_found, interface_state, member_line,
                    member_state, members_view, state_line)
from nso_facts.topology.interfaces import interfaces_match
from nso_facts.topology.physical import parse_interfaces_brief


def _lines_for(text, ac):
    """The lines of a bulk reply that describe ``ac``, or a note that there are none."""
    found = [line for line in str(text).splitlines() if line.split() and interfaces_match(ac, line.split()[0])]
    return "\n".join(found) if found else f"(no line for {ac} in the answer)"


def bridge_identities(text):
    """Every ``group:domain`` a reply names."""
    return [f"{bg}:{bd}" for bg, bd in re.findall(r"Bridge group: ([^,\s]+), bridge-domain: ([^,\s]+),", str(text))]


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
            state_read, state_shown = brief, _lines_for(brief, ac)
            state_found = f"{ac}: admin {matches[0]['admin']}, oper {matches[0]['oper']}"
        else:
            state_read = await probe.show(dev, f"interfaces {ac}")
            status, state_shown = interface_state(state_read, ac), None
            state_found = interface_found(state_read, ac)
        probe.check("attachment", status, ac, dev, read=state_read, shown=state_shown,
                    matched=state_line(state_read, ac) if state_shown is None else None, found=state_found)
        bulk = await probe.show(dev, "l2vpn bridge-domain detail", required=False)
        blocks = re.split(r"(?=Bridge group: )", bulk)
        selected = [b for b in blocks if bridge_identity(b) and member_state(b, ac) != "unknown"]
        if len(selected) > 1:
            probe.check("bridge_identity", "unknown", f"Ambiguous bridge membership for {ac}", dev, read=bulk,
                        shown="\n".join(b.splitlines()[0] for b in selected if b.strip()),
                        found=f"{len(selected)} bridge-domains list {ac}: "
                              + ", ".join(":".join(bridge_identity(b)) for b in selected))
            continue
        # The bridge's block is cut from a read; the verdicts name that whole read.
        if len(selected) == 1:
            detail = selected[0]
            detail_read = bulk
            identity = bridge_identity(detail)
            lookup = bulk  # retains observed RP location for forwarding query
        else:
            lookup = await probe.show(dev, f"l2vpn bridge-domain interface {ac}")
            identity = bridge_identity(lookup)
            if not identity:
                domains_named = bridge_identities(lookup)
                probe.check("bridge_identity", "unknown", f"Exact bridge identity not established for {ac}", dev,
                            read=[bulk, lookup],
                            found=(f"{len(domains_named)} bridge-domains in the reply for {ac}: " + ", ".join(domains_named))
                            if len(domains_named) > 1 else
                            f"no bridge-domain found for {ac}" + (f" (the reply begins: {first_line(lookup)})" if first_line(lookup) else ""))
                continue
            bg, bd = identity
            detail_read = await probe.show(dev, f"l2vpn bridge-domain group {bg} detail")
            blocks = [b for b in re.split(r"(?=Bridge group: )", detail_read) if bridge_identity(b) == identity]
            detail = blocks[0] if len(blocks) == 1 else ""
        bg, bd = identity
        if dev in domains and domains[dev]["identity"] != identity:
            probe.check("membership", "unknown", "Service attachments span multiple bridge domains; requires investigation", dev,
                        read=[domains[dev]["read"], detail_read],
                        found=f"{':'.join(domains[dev]['identity'])} and {bg}:{bd} both hold this service's attachments")
        state = re.search(r"bridge-domain: [^,]+, id: \d+, state: (up|down)\b", detail, re.I)
        probe.check("bridge_state", "pass" if state and state[1].lower() == "up" else "fault" if state else "unknown", f"{bg}:{bd}", dev,
                    read=detail_read, shown=bridge_view(detail) or None,
                    found=f"state: {state[1].lower()}" if state else "no state in the bridge-domain header")
        row = member_line(detail, ac)
        count = sum(1 for m in re.finditer(r"^\s+(?:AC: )?([\w/.-]+), state(?: is|:) (?:up|down)\b", detail, re.M | re.I)
                    if interfaces_match(ac, m[1]))
        probe.check("membership", member_state(detail, ac), f"{ac} in {bg}:{bd}", dev, read=detail_read, shown=members_view(detail) or None,
                    found=row.strip() if count == 1 else f"{ac} is listed {count} times in {bg}:{bd}" if count else
                    f"{ac} is not listed among the members of {bg}:{bd}")
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
        ranges = re.search(r"VLAN ranges: (\[[^\n]*\])", member)
        counted = re.search(r"Num Ranges: (\d+)", member)
        seen = (f"no AC entry for {ac} in {bg}:{bd}" if not member else
                "QinQ is not checked by the fixed check" if ep.get("inner_vlan") is not None else
                f"VLAN ranges {ranges[1]}; intended {vlan}" if ranges else
                f"Num Ranges: {counted[1]}; intended {vlan}" if counted else f"no VLAN range line; intended {vlan}")
        probe.check("encapsulation", "pass" if encapsulation else "unknown", f"{ac} intended VLAN {vlan}", dev,
                    read=detail_read, shown=member or bridge_view(detail) or None, found=seen)
        loc = re.search(r"(?:^|\n)([A-Za-z]+/\d+/RP\d+/CPU\d+):", lookup)
        # Without the route processor's location, the forwarding verdicts rest on the read that lacked it.
        forwarding = lookup if not loc else await probe.show(
            dev, f"l2vpn forwarding bridge-domain {bg}:{bd} detail location {loc[1].split('/', 1)[1]}")
        valid = bool(loc and re.search(rf"Bridge-domain name: {re.escape(bg)}:{re.escape(bd)}, id: \d+, state: up\b", forwarding))
        member_row = member_line(forwarding, ac, True)
        probe.check("local_forwarding", member_state(forwarding, ac, True) if valid else "unknown", f"Programmed bridge member {ac}", dev,
                    read=forwarding, matched=member_row,
                    shown=None if member_row else focus(forwarding, [ac]),
                    found=("the route processor location was not found in the bridge-domain reply" if not loc else
                           f"the reply is not the up forwarding table of {bg}:{bd}"
                           + (f" (it begins: {first_line(forwarding)})" if first_line(forwarding) else " (it holds nothing)") if not valid else
                           member_row.strip() if member_row else f"no forwarding entry for {ac} in the reply"))
        # Zero MACs can be healthy only with a demonstrated local flood path.
        flood = bool(re.search(r"Broadcast & Multicast: enabled", forwarding) and re.search(r"Unknown unicast: enabled", forwarding))
        if not routed:
            lines = [l.strip() for l in forwarding.splitlines() if "Broadcast & Multicast" in l or "Unknown unicast" in l]
            probe.check("local_flooding", "pass" if valid and flood else "unknown", f"Flooding in {bg}:{bd}", dev,
                        read=forwarding, shown=focus(forwarding, ["Broadcast & Multicast", "Unknown unicast"]),
                        found="; ".join(lines) if lines else "no flooding lines in the forwarding reply")
        if not routed and re.search(r"Split Horizon Group: (?!none)[^\n]+|E-Tree: Leaf", detail, re.I):
            probe.check("switching_policy", "unknown", "Non-default switching restrictions require investigation", dev,
                        read=detail_read, shown=bridge_view(detail) or None)
        evpn_state = re.search(r"EVPN, state: (up|down)\b", detail, re.I)
        if evpn_state:
            probe.check("evpn_binding", "pass" if evpn_state[1].lower() == "up" else "fault", f"{bg}:{bd}", dev,
                        read=detail_read, shown=bridge_view(detail) or None, found=f"EVPN, state: {evpn_state[1].lower()}")
        evi = re.search(r"EVI: (\d+)(?:[^\n]*state[^\n]*up)?", detail, re.I)
        domains[dev] = {"identity": identity, "detail": detail, "read": detail_read, "evi": evi[1] if evi else None}
    return domains
