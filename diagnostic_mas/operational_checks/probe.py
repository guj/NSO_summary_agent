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


class Probe:
    def __init__(self, client, call, cache, max_calls=24):
        self.client, self.call, self.cache = client, call, cache
        self.max_calls = max_calls
        self.calls = 0
        self.checks = []
        self.sources = []

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
                self.check("collection_budget", "unknown", "Basic-check tool allowance exhausted", device)
                return ""
            self.calls += 1
            try:
                raw = await self.call(self.client, "exec_show", {"device_name": device, "input_command": command})
                data = unwrap_mcp_data(raw)
                text = data.get("result", "") if isinstance(data, dict) else ""
                if mcp_is_error(raw) or not isinstance(text, str) or ERROR.search(text):
                    text = ""
            except Exception:
                text = ""
            self.cache[key] = (text.replace("\r", ""), datetime.now(timezone.utc).isoformat())
        text, stamp = self.cache[key]
        self.sources.append({"device": device, "tool": "exec_show", "command": command,
                             "collected_at": stamp, "available": bool(text)})
        if not text and required:
            self.check("collection", "unknown", "Unavailable/unsupported response: " + command, device)
        return text

    def check(self, name, status, observation, device=None):
        self.checks.append({"check": name, "status": status, "device": device, "observation": observation})


def interface_state(text, interface):
    for m in re.finditer(r"^([\w/.-]+) is (administratively down|up|down), line protocol is (up|down)", text, re.M | re.I):
        if interfaces_match(interface, m[1]):
            return "pass" if m[2].lower() == m[3].lower() == "up" else "fault"
    return "unknown"


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
