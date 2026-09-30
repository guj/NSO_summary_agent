"""Service-scoped L3RT attachments, gateway and all border dependencies."""
import ipaddress
import re
from .bridge import collect
from .probe import attachments, cef_ready, interface_state, safe


def route_prefix(text, target):
    try:
        address = ipaddress.ip_interface(target).ip
    except ValueError:
        return None
    entries = re.findall(r"Routing entry for ([0-9a-fA-F:./]+)", text)
    if len(entries) != 1:
        return None
    try:
        network = ipaddress.ip_network(entries[0], strict=False)
        return str(network) if address in network else None
    except ValueError:
        return None


async def check(instance, probe):
    local = instance.get("device")
    if not safe(local):
        probe.check("local_identity", "unknown", "Local attachment device missing")
        return
    eps = attachments(instance)
    if not eps or any(ep["device"] != local for ep in eps):
        probe.check("attachments", "unknown", "Unsupported attachment topology")
        return
    domains = await collect(instance, probe, routed=True)
    domain = domains.get(local)
    bvis = set(re.findall(r"AC: (BVI\d+), state is", domain["detail"])) if domain else set()
    if len(bvis) != 1:
        probe.check("gateway_identity", "unknown", "One exact gateway binding not established; direct/routed variants need investigation", local)
        return
    gateway = bvis.pop()
    text = await probe.show(local, f"interfaces {gateway}")
    probe.check("gateway", interface_state(text, gateway), gateway, local)
    config = await probe.show(local, f"running-config interface {gateway}")
    if not re.search(rf"^interface {re.escape(gateway)}\s*$", config, re.M):
        probe.check("routing_context", "unknown", "Scoped gateway configuration unavailable", local)
        return
    vrf = re.search(r"^\s+vrf (\S+)\s*$", config, re.M)
    context = vrf[1] if vrf else None
    if context and not safe(context):
        probe.check("routing_context", "unknown", "Unsupported VRF identifier", local)
        return
    targets = {}
    for version in (4, 6):
        intent = instance.get(f"gateway-ipv{version}")
        if intent:
            try:
                net = ipaddress.ip_interface(f"{intent['address']}/{intent.get('netmask', intent.get('prefix-length'))}")
                if net.version != version:
                    raise ValueError()
                targets.setdefault(version, []).append(str(net.network))
                # Exact configured address, not a similarly named gateway.
                addresses = re.findall(r"\b[0-9a-fA-F:.]+(?:/\d+)?", config)
                present = any(value.split("/")[0] == str(net.ip) for value in addresses)
                probe.check("gateway_address", "pass" if present else "unknown", str(net), local)
            except (ValueError, KeyError, TypeError):
                probe.check("gateway_intent", "unknown", f"IPv{version} gateway prefix incomplete", local)
    external = instance.get("external-access") or {}
    borders = [item.get("device") for item in external.get("border-router", []) if isinstance(item, dict)]
    for version in (4, 6):
        for item in external.get(f"permit-ipv{version}", []):
            try:
                address = ipaddress.ip_interface(item["address"])
                if address.version != version:
                    raise ValueError()
                targets.setdefault(version, []).append(str(address))
            except (ValueError, KeyError, TypeError):
                probe.check("route_intent", "unknown", f"Invalid IPv{version} destination")
    family = external.get("ip-type")
    required = {4} if family == "v4" else {6} if family == "v6" else {4, 6} if family in {"both", "dual", "v4v6"} else set()
    if external and (not borders or not required):
        probe.check("external_intent", "unknown", "Border roles or address families not established")
    for version in required - targets.keys():
        probe.check("route_intent", "unknown", f"No service destination for required IPv{version}")
    if not targets:
        probe.check("route_intent", "unknown", "No required service prefixes discovered")
    # A border VRF cannot be assumed to match the local gateway context.
    if borders and context:
        probe.check("border_context", "unknown", "Border VRF mapping requires investigation")
        return
    devices = [local] + list(dict.fromkeys(borders))
    for dev in devices:
        if not safe(dev):
            probe.check("border_identity", "unknown", "Invalid border device identity")
            continue
        for version, prefixes in targets.items():
            wanted = list(dict.fromkeys(prefixes))
            if external:
                wanted.append("0.0.0.0/0" if version == 4 else "::/0")
            for target in wanted:
                af = "ipv6 " if version == 6 else ""
                network = ipaddress.ip_network(target, strict=False)
                lookup = str(network.network_address) if network.prefixlen == network.max_prefixlen else target
                ctx = f"vrf {context} " if context else ""
                route = await probe.show(dev, f"route {ctx}{af}{lookup}")
                prefix = route_prefix(route, target)
                # Default-only coverage is not evidence of the customer return route.
                if prefix and ipaddress.ip_network(prefix).prefixlen == 0 and target not in {"0.0.0.0/0", "::/0"}:
                    prefix = None
                probe.check("rib", "pass" if prefix else "unknown", f"IPv{version} {target} in {context or 'global'}", dev)
                if not prefix:
                    continue
                cef = await probe.show(dev, f"cef {ctx}{'ipv6 ' if version == 6 else ''}{prefix}")
                connected = dev == local and bool(re.search(r'Known via "connected"', route)) and gateway in route
                if dev == local and target not in {"0.0.0.0/0", "::/0"}:
                    probe.check("gateway_route_binding", "pass" if connected else "unknown", f"{prefix} bound to {gateway}", dev)
                ok = cef_ready(cef, prefix, connected=connected)
                if connected:
                    ok = ok and gateway in cef
                probe.check("fib", "pass" if ok else "unknown", f"IPv{version} {prefix} in {context or 'global'}", dev)
