"""Interface name normalization (IOS-XR show vs config naming).

NSO/YANG config uses long names (HundredGigE…); show parsers return
abbreviated CLI names (Hu…). See nso_facts/topology/docs/DESIGN.md
#ios-xr-interface-names-nso-config-vs-show-output.
"""

from __future__ import annotations

# Longest prefix first when expanding abbreviated CLI names.
_IOS_INTERFACE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("FourHundredGigE", "FH"),
    ("TwentyFiveGigE", "TF"),
    ("HundredGigE", "Hu"),
    ("FortyGigE", "Fo"),
    ("TenGigE", "Te"),
    ("GigabitEthernet", "Gi"),
    ("Bundle-Ether", "BE"),
    ("Loopback", "Lo"),
    ("MgmtEth", "Mg"),
    ("BVI", "BV"),
    ("Null", "Nu"),
)


def _known_long_name(name: str) -> tuple[str, str] | None:
    """The table row whose long name ``name`` already carries, if any."""
    for row in _IOS_INTERFACE_PREFIXES:
        if name.startswith(row[0]):
            return row
    return None


def interface_variants(name: str) -> set[str]:
    """Return equivalent IOS-XR interface name forms (long + short)."""
    known = _known_long_name(name)
    if known:
        # Already long: "FourHundredGigE…" is not the "Fo" abbreviation of FortyGigE.
        return {name, known[1] + name[len(known[0]) :]}
    variants = {name}
    for long_prefix, short_prefix in _IOS_INTERFACE_PREFIXES:
        if name.startswith(short_prefix):
            remainder = name[len(short_prefix) :]
            if remainder:
                variants.add(long_prefix + remainder)
    return variants


def interfaces_match(left: str, right: str) -> bool:
    return bool(interface_variants(left) & interface_variants(right))


def expand_interface_name(name: str) -> str:
    """Prefer long IOS-XR form (e.g. Hu0/0/0/0 → HundredGigE0/0/0/0)."""
    if _known_long_name(name):
        return name
    for long_prefix, short_prefix in _IOS_INTERFACE_PREFIXES:
        if name.startswith(short_prefix):
            remainder = name[len(short_prefix) :]
            if remainder:
                return long_prefix + remainder
    return name


# Interface types that are not a port and have none of their own to inspect.
_NO_PORT_OF_ITS_OWN = ("BVI", "Loopback", "Null")


def has_own_port(name: str) -> bool:
    """False for a BVI, Loopback or Null interface; True for every other name, a bundle included."""
    return not expand_interface_name(name).startswith(_NO_PORT_OF_ITS_OWN)


def canonical_interface_name(
    device: str,
    interface: str,
    physical_by_device: dict[str, set[str]] | None = None,
) -> str:
    """Resolve to configured name from physical layer when possible."""
    if physical_by_device:
        for candidate in physical_by_device.get(device, set()):
            if interfaces_match(interface, candidate):
                return candidate
    return expand_interface_name(interface)


def physical_interfaces_by_device(
    physical_edges: list[dict],
) -> dict[str, set[str]]:
    by_device: dict[str, set[str]] = {}
    for edge in physical_edges:
        local = edge.get("local") or {}
        device = local.get("device")
        interface = local.get("interface")
        if isinstance(device, str) and isinstance(interface, str):
            by_device.setdefault(device, set()).add(interface)
    return by_device
