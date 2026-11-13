"""Bounded read-only collection and conservative IOS-XR output normalization.

Only a recognized, scoped output is evidence. Unsupported/truncated responses
remain unknown. Cache lifetime is one collection; never reuse a previous run.
"""
from datetime import datetime, timezone
import re
from nso_facts.mcp_client import mcp_is_error, unwrap_mcp_data
from nso_facts.l2vpn_xconnect import ac_name_from_endpoint
from nso_facts.topology.interfaces import interfaces_match

TOKEN = re.compile(r"[A-Za-z0-9_./:-]+")
ERROR = re.compile(r"invalid input|syntax error|unknown command|incomplete command|ambiguous command|truncated|timed out|authorization failed", re.I)


def safe(value):
    return isinstance(value, str) and bool(TOKEN.fullmatch(value))


def attachments(instance):
    out = []
    def visit(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get("device"), str) and "interface" in obj:
                items = obj["interface"]
                for interface in items if isinstance(items, list) else [items]:
                    ac = ac_name_from_endpoint(interface)
                    if ac and safe(ac) and safe(obj["device"]):
                        ep = {"device": obj["device"], "ac": ac, "vlan": interface.get("outervlan"), "inner_vlan": interface.get("innervlan")}
                        if ep not in out:
                            out.append(ep)
            for val in obj.values():
                if isinstance(val, (dict, list)):
                    visit(val)
        elif isinstance(obj, list):
            for val in obj:
                visit(val)
    visit(instance)
    return out


# What a verdict that did not pass keeps of the device's answer: enough to see why, not the whole reply.
# A read gets up to EXCERPT_LIMIT characters; a verdict that read several shares EXCERPT_TOTAL between them.
EXCERPT_LIMIT = 2500
EXCERPT_TOTAL = 6000
EXCERPT_MIN_SHARE = 1200


class Reading(str):
    """A command's output that also names the device and command it came from.

    ``shown`` is what the device (or NSO) answered, kept for a report when a verdict from this read
    did not pass: for an answer the checks cannot use, such as an error, ``self`` is empty but
    ``shown`` has the reason.
    """

    def __new__(cls, text, device, command, collected_at, shown=None):
        reading = super().__new__(cls, text)
        reading.source = {"device": device, "command": command,
                          "collected_at": collected_at, "available": bool(text)}
        reading.shown = text if shown is None else shown
        return reading


# What each check looks for, in plain words: shown with a verdict that did not pass, so that a missing line
# (nothing to point at) still says what was expected.
LOOKED_FOR = {
    "attachment": "the attachment's interface up, line protocol up",
    "bridge_identity": "exactly one bridge group and bridge-domain for the attachment",
    "bridge_state": "the bridge-domain's state: up",
    "membership": "the attachment listed among the bridge-domain's members, state up",
    "encapsulation": "the intended VLAN on the attachment's entry in the bridge-domain",
    "local_forwarding": "the attachment programmed as a forwarding member, oper up",
    "local_flooding": "broadcast, multicast and unknown unicast flooding enabled",
    "switching_policy": "no split-horizon group or leaf role restricting the bridge-domain",
    "evpn_binding": "the bridge-domain's EVPN state: up",
    "gateway_identity": "exactly one BVI among the bridge-domain's members (an \"AC: BVI…\" line)",
    "gateway": "the gateway BVI up, line protocol up",
    "gateway_address": "the intended gateway address configured on the BVI",
    "routing_context": "the BVI's configuration, and a VRF name the check can use",
    "rib": "a route for the prefix in the routing table (the default route alone does not count)",
    "gateway_route_binding": "the prefix as a connected route bound to this service's gateway BVI",
    "gateway_unique": "the gateway address configured on this service only, or its route bound to this service's BVI",
    "fib": "a ready CEF entry for the prefix, with an installed next hop",
    "session": "a mirror session whose destination is the service's destination interface",
    "source": "the source listed under that session, mirrored in the intended direction, status Operational",
    "interface": "the interface up, line protocol up",
    "effective_evpn": "the EVI's route targets, PE identity and label",
    "rt_compatibility": "the destination's export route targets matching the source's import",
    "replication": "the remote replication label and next hop matching the receiving PE",
    "transport": "a resolved, labeled CEF path to the remote PE",
    "service_labels": "the xconnect's service labels in its detail",
    "label_path": "the sender's label for the far end matching the receiver's own label",
    "collection": "a usable answer to the command",
}


def bridge_view(block, limit=EXCERPT_LIMIT):
    """A bridge-domain's block as a report shows it: the header, then the members.

    IOS XR writes about thirty lines of settings before the lines that matter (``ACs: 2 (2 up)…``,
    ``List of EVPNs``, ``List of ACs``), so a cut at the start would keep the settings and lose them.
    A short block, or one with no such lines, is shown as it is.
    """
    block = str(block or "")
    if len(block) <= limit:
        return block
    lines = block.splitlines()
    start = next((n for n, line in enumerate(lines) if re.match(r"\s+(ACs: \d+|List of )", line)), None)
    if not start:
        return block
    return lines[0] + "\n  …\n" + "\n".join(lines[start:])


def focus(text, terms, limit=EXCERPT_LIMIT, context=1):
    """``text`` as a report shows it when it is longer than ``limit``: its first line and each line
    that names one of ``terms`` with ``context`` lines either side, gaps marked "…". A short text, or
    one that names none of them, is returned as it is."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    wanted = [str(t).lower() for t in terms if t]
    hits = [n for n, line in enumerate(lines) if any(t in line.lower() for t in wanted)]
    if not hits:
        return text
    keep = sorted({0} | {m for n in hits for m in range(n - context, n + context + 1) if 0 <= m < len(lines)})
    out, last = [], -1
    for n in keep:
        if last >= 0 and n > last + 1:
            out.append("  …")
        out.append(lines[n])
        last = n
    return "\n".join(out)


def members_view(block):
    """A bridge-domain's block reduced to who its members are: the header, the member counts, the EVPN lines and
    each member's own ``AC:`` / ``PW:`` / ``VFI:`` line, without the settings printed under each.

    For a verdict about identity (which interfaces are in it, is one a BVI), where every member's
    settings would use up the allowance before the last member is listed. Returned as it is when the block has
    no member lines to go by.
    """
    block = str(block or "")
    lines = block.splitlines()
    marker = next((n for n, line in enumerate(lines) if re.match(r"\s+(ACs: \d+|List of )", line)), None)
    if not marker:
        return block
    wanted = re.compile(r"\s*(ACs: \d+|List of |AC: |PW: |VFI: |EVPN, state|evi: |PBB|VNI)")
    return "\n".join([lines[0], "  …"] + [line for line in lines[marker:] if wanted.match(line)])


def excerpt(text, limit=EXCERPT_LIMIT):
    """(text cut to ``limit`` between lines, whether it was cut)."""
    text = str(text or "").strip("\n").rstrip()
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    line_end = cut.rfind("\n")
    # Between lines, unless that would throw most of the allowance away (one very long line).
    return (cut[:line_end] if line_end >= limit // 2 else cut).rstrip(), True


def _sources(read):
    """The distinct reads behind one verdict, from a Reading or a list of them."""
    found = []
    for item in read if isinstance(read, (list, tuple)) else [read]:
        source = getattr(item, "source", None)
        if source and source not in found:
            found.append(source)
    return found


class Probe:
    def __init__(self, client, call, cache, max_calls=24):
        self.client, self.call, self.cache = client, call, cache
        self.max_calls = max_calls
        self.calls = 0
        self.checks = []
        self.sources = []
        # (device, command) -> the MCP error code of a failed command, e.g. exec_unavailable.
        self.error_codes = {}
        # Facts the collector worked out across all services, for checks that need them.
        self.fleet = {}

    async def show(self, device, command, *, required=True):
        if hasattr(self.cache, "locks"):
            import asyncio
            lock = self.cache.locks.setdefault((device, command), asyncio.Lock())
            async with lock:
                return await self._show(device, command, required=required)
        return await self._show(device, command, required=required)

    async def _show(self, device, command, *, required=True):
        key = (device, command)
        if key not in self.cache:
            if self.calls >= self.max_calls:
                unread = Reading("", device, command, None)
                self.check("collection_budget", "unknown", "Basic-check tool allowance exhausted", device, read=unread)
                return unread
            self.calls += 1
            shown = None
            try:
                raw = await self.call(self.client, "exec_show", {"device_name": device, "input_command": command})
                data = unwrap_mcp_data(raw)
                text = data.get("result", "") if isinstance(data, dict) else ""
                if mcp_is_error(raw) or not isinstance(text, str) or ERROR.search(text):
                    # Not evidence for a verdict, but what came back says why.
                    shown = (str(raw.get("error_message") or "") if mcp_is_error(raw) and isinstance(raw, dict)
                             else text if isinstance(text, str) else "")
                    text = ""
                if mcp_is_error(raw) and isinstance(raw, dict) and raw.get("error_code"):
                    self.error_codes[key] = raw["error_code"]
            except Exception as exc:
                text, shown = "", f"The read failed: {type(exc).__name__}"
            self.cache[key] = (text.replace("\r", ""), datetime.now(timezone.utc).isoformat(),
                               None if shown is None else shown.replace("\r", ""))
        text, stamp, shown = self.cache[key]
        self.sources.append({"device": device, "tool": "exec_show", "command": command,
                             "collected_at": stamp, "available": bool(text)})
        reading = Reading(text, device, command, stamp, shown)
        if not text and required:
            self.check("collection", "unknown", "Unavailable/unsupported response: " + command, device, read=reading)
        return reading

    def check(self, name, status, observation, device=None, *, conclusive=False, read=None, shown=None, matched=None,
              found=None):
        """Record one observation; ``conclusive`` marks a fault this run's readings show in full.

        ``read`` is the output (or outputs) the verdict was taken from, as returned
        by ``show``; their commands are kept with it. A verdict from the service
        definition alone has none. A verdict that did not pass also keeps what the device answered
        (cut to ``EXCERPT_LIMIT``), so a report can show why; ``shown`` names the part of one
        read the verdict was taken from, when that is less than the whole answer, and ``matched`` the
        line the verdict was read from (shown with three lines either side). It also records what the
        check looked for, in words, and ``found`` (given by the caller from the reply) what it saw instead.
        """
        record = {"check": name, "status": status, "device": device, "observation": observation}
        if conclusive:
            record["conclusive"] = True
        if status != "pass" and name in LOOKED_FOR:
            record["looked_for"] = LOOKED_FOR[name]
        if status != "pass" and found:
            record["found"] = str(found).strip()
        readings = [r for r in (read if isinstance(read, (list, tuple)) else [read]) if getattr(r, "source", None)]
        sources = _sources(read)
        if sources and status != "pass":
            originals, sources = sources, [dict(source) for source in sources]
            answers = {id(r.source): r for r in readings}
            share = min(EXCERPT_LIMIT, max(EXCERPT_MIN_SHARE, EXCERPT_TOTAL // len(sources)))   # several reads share it
            for original, kept in zip(originals, sources):
                text = shown if shown is not None and len(sources) == 1 else getattr(answers.get(id(original)), "shown", "")
                if matched and shown is None:
                    text = focus(text, [matched], EXCERPT_LIMIT, context=3)
                cut, was_cut = excerpt(text, EXCERPT_LIMIT if len(sources) == 1 else share)
                if cut:
                    kept["excerpt"] = cut
                    if was_cut:
                        kept["truncated"] = True
        if sources:
            record["sources"] = sources
        self.checks.append(record)


def interface_state(text, interface):
    for m in re.finditer(r"^([\w/.-]+) is (administratively down|up|down), line protocol is (up|down)", text, re.M | re.I):
        if interfaces_match(interface, m[1]):
            return "pass" if m[2].lower() == m[3].lower() == "up" else "fault"
    return "unknown"


def state_line(text, interface):
    """The line of ``show interfaces`` output that gives ``interface``'s state, or None."""
    for m in re.finditer(r"^([\w/.-]+) is (administratively down|up|down), line protocol is (up|down).*$", text, re.M | re.I):
        if interfaces_match(interface, m[1]):
            return m[0]
    return None


def member_line(text, interface, forwarding=False):
    """The line that gives ``interface``'s state as a bridge member (as ``member_state`` reads it), or None."""
    pattern = (r"^\s+([\w/.-]+), state: oper (?:up|down)\b.*$" if forwarding else
               r"^\s+(?:AC: )?([\w/.-]+), state(?: is|:) (?:up|down)\b.*$")
    return next((m[0] for m in re.finditer(pattern, text, re.M | re.I) if interfaces_match(interface, m[1])), None)


# The clock line IOS XR prints before every reply, and the prompt after it: not what the device said.
_FRAME = re.compile(r"^(?:[A-Z][a-z]{2} [A-Z][a-z]{2} +\d+ [\d:.]+ \w+|\S+:\S+#)$")


def first_line(text, limit=160):
    """The first line of a reply that the device said (cut), or None: what it said when it had nothing to show."""
    line = next((l.strip() for l in str(text or "").splitlines() if l.strip() and not _FRAME.match(l.strip())), "")
    return (line[:limit] + "…" if len(line) > limit else line) or None


def interface_found(text, interface):
    """What a ``show interfaces`` reply said about ``interface``: its state line, or that it had none."""
    line = state_line(text, interface)
    if line:
        return line
    said = first_line(text)
    return f"no state line for {interface} in the reply" + (f" (it begins: {said})" if said else " (empty reply)")


def bridge_identity(text):
    matches = re.findall(r"Bridge group: ([^,\s]+), bridge-domain: ([^,\s]+),", text)
    return matches[0] if len(matches) == 1 and all(safe(x) for x in matches[0]) else None


def member_state(text, interface, forwarding=False):
    pattern = (r"^\s+([\w/.-]+), state: oper (up|down)\b" if forwarding else
               r"^\s+(?:AC: )?([\w/.-]+), state(?: is|:) (up|down)\b")
    states = [m[2].lower() for m in re.finditer(pattern, text, re.M | re.I) if interfaces_match(interface, m[1])]
    return ("pass" if states[0] == "up" else "fault") if len(states) == 1 else "unknown"


def cef_ready(text, prefix, *, labeled=False, connected=False):
    # Require a prefix header and an installed egress, not mere route existence.
    import ipaddress
    try:
        wanted = ipaddress.ip_network(prefix, strict=False)
    except ValueError:
        return False
    headers = re.findall(r"^([0-9a-fA-F:.]+/\d+), version ", text, re.M)
    try:
        matches = any(ipaddress.ip_network(h, strict=False) == wanted for h in headers)
    except ValueError:
        return False
    if not matches:
        return False
    if re.search(r"unresolved|drop adjacency|discard|Null0|punt", text, re.I):
        return False
    if labeled:
        return bool(re.search(r"labels imposed \{(?:\d+|ImplNull)(?:\s+(?:\d+|ImplNull))*\}", text, re.I) and re.search(r"via [^\n]+, [A-Za-z][A-Za-z-]*[\d/]", text)
                    and ("ImplNull" not in text or re.search(r"local adjacency|^\s*\d+\s+Y\s+", text, re.M)))
    if connected and re.search(r"attached|glean|local adjacency", text, re.I):
        return True
    return bool(re.search(r"^\s*\d+\s+Y\s+\S+\s+", text, re.M) or re.search(r"next hop [^\n]+\n\s+local adjacency", text, re.I))
