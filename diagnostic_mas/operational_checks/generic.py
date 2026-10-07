"""Interface-only check for service types that have no module of their own.

A port that is down is evidence of a fault. Ports that are up say nothing about
the service's own logic, so this check can report Down or Unknown, never Up.
"""
from nso_facts.l2vpn_xconnect import ac_name_from_endpoint
from .probe import interface_state, safe


def interfaces(instance):
    """(device, interface) for each interface the instance names.

    Service models here share one interface shape (``type`` and ``id``, with
    optional VLAN tags) under whichever device entry contains it.
    """
    found = []
    def visit(obj, device):
        if isinstance(obj, dict):
            if isinstance(obj.get("device"), str):
                device = obj["device"]
            name = ac_name_from_endpoint(obj)
            if name and safe(name) and device and safe(device) and (device, name) not in found:
                found.append((device, name))
            for value in obj.values():
                visit(value, device)
        elif isinstance(obj, list):
            for value in obj:
                visit(value, device)
    visit(instance, None)
    return found


async def check_interfaces(probe, pairs):
    for device, name in pairs:
        probe.check("interface", interface_state(await probe.show(device, f"interfaces {name}"), name), name, device)


async def check(instance, probe):
    pairs = interfaces(instance)
    if not pairs:
        probe.check("interfaces", "unknown", "No interface could be identified in this service")
    await check_interfaces(probe, pairs)
    probe.check("service_logic", "unknown", "No checks for this service type; only its interfaces were examined")
