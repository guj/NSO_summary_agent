"""Per-device interface and hardware detail for the HTML report's device blocks."""

from __future__ import annotations

from typing import Any

from nso_facts.fleet_rollups import (
    devices_in_edges,
    edge_status,
    merged_edges,
    mismatch_by_edge_id,
    operational_issues,
    operational_layers,
    static_layers,
    unmapped_by_device,
)
from nso_facts.topology.iface_equiv import compact_box_list

INVENTORY_REVIEW_STATUS = "Inventory Review"

_I1 = "  "
_I2 = "    "


def device_detail_lines(
    topology: Any,
    *,
    hardware_health: dict[str, Any] | None = None,
    extra_devices: list[str] | None = None,
    only_devices: list[str] | None = None,
    live_verified: list[str] | None = None,
) -> dict[str, list[str]]:
    """Interfaces, hardware and exceptions text per device (no status, routing or services).

    ``live_verified`` names the devices that answered a live query. For any other device
    the interface state was not collected, which is reported as such rather than as
    inventory mismatches. ``None`` skips that check. Devices with nothing to show are omitted.
    """
    from nso_facts.hardware_health import format_device_hardware_section

    op_layers = operational_layers(topology)
    if op_layers is None:
        return {}
    static = static_layers(topology)
    phys = merged_edges(op_layers, static, "physical")
    under = merged_edges(op_layers, static, "underlay")
    route = merged_edges(op_layers, static, "routing")
    unmapped_bgp, unmapped_isis = unmapped_by_device(topology)
    unexpected_live = _unexpected_live_by_device(topology)
    mismatch_by_edge = mismatch_by_edge_id(topology)
    hw = hardware_health if isinstance(hardware_health, dict) else {}
    devices = (
        devices_in_edges(phys, under, route)
        | set(unmapped_bgp)
        | set(unmapped_isis)
        | set(unexpected_live)
        | set(hw)
        | {n.strip() for n in (extra_devices or []) if isinstance(n, str) and n.strip()}
    )
    if only_devices:
        devices = {n.strip() for n in only_devices if isinstance(n, str) and n.strip()}
    verified = None if live_verified is None else set(live_verified)

    details: dict[str, list[str]] = {}
    for device in sorted(devices):
        has_interfaces = _iface_pair_counts(device, phys)["total"] > 0
        collected = verified is None or device in verified
        exceptions = _exceptions_section(
            device,
            phys=phys if collected else [],
            mismatch_by_edge=mismatch_by_edge,
            unmapped_bgp=unmapped_bgp.get(device, []),
            unmapped_isis=unmapped_isis.get(device, []),
            unexpected_live=unexpected_live.get(device, []),
            troubleshoot=_troubleshooting_for_device(topology, device),
        )
        if not (has_interfaces or hw.get(device) or exceptions):
            continue
        if collected:
            lines = _interfaces_summary_section(device, phys)
        else:
            lines = [
                "Interfaces",
                "----------",
                f"{_I1}Interface state was not collected for this device in this run.",
            ]
        lines.append("")
        lines.extend(format_device_hardware_section(hw.get(device)))
        if exceptions:
            lines.append("")
            lines.extend(exceptions)
        details[device] = lines
    return details


def _interfaces_summary_section(device: str, phys: list) -> list[str]:
    counts = _iface_pair_counts(device, phys)
    lines = [
        "Interfaces",
        "----------",
        f"{_I1}Summary:",
        f"{_I2}Total:       {counts['total']}",
        f"{_I2}Up/Up:       {counts.get('up/up', 0)}",
        f"{_I2}Down/Down:   {counts.get('down/down', 0)}",
        f"{_I2}Admin Down:  {counts.get('admin-down', 0)}",
        f"{_I2}Mapping Unknown: {counts.get('unknown', 0)}",
    ]
    # Include up/down in summary if present
    if counts.get("up/down"):
        lines.insert(5, f"{_I2}Up/down:     {counts['up/down']}")
    if counts.get("no-state"):
        lines.append(f"{_I2}No state:    {counts['no-state']}")
    if counts.get("not-read"):
        lines.append(f"{_I2}Not read:    {counts['not-read']}")
    return lines


def _iface_pair_counts(device: str, phys: list) -> dict[str, int]:
    pair_vals: list[str] = []
    for e in phys:
        if not isinstance(e, dict):
            continue
        if (e.get("local") or {}).get("device") != device:
            continue
        pair_vals.append(_iface_pair_status(e))
    return _count_statuses(pair_vals) if pair_vals else {"total": 0}


def _exceptions_section(
    device: str,
    *,
    phys: list,
    mismatch_by_edge: dict[str, dict[str, Any]],
    unmapped_bgp: list[str],
    unmapped_isis: list[str],
    unexpected_live: list[str],
    troubleshoot: dict[str, Any] | None,
) -> list[str]:
    inventory = _inventory_mismatch_lines(device, phys, mismatch_by_edge)
    up_down = _interface_up_down_formatted(device, phys, troubleshoot)
    down_down = _interface_down_down_formatted(device, phys)
    live_extra = _names_four_per_row(sorted(set(unexpected_live)), indent=_I2)
    parts: list[tuple[str, list[str]]] = []
    if inventory:
        parts.append(("Inventory mismatch:", [f"{_I2}{x}" for x in inventory]))
    if up_down:
        parts.append(("Interface up/down:", up_down))
    if down_down:
        parts.append(("Interface down/down:", down_down))
    if live_extra:
        parts.append(("Live interface not in static config:", live_extra))
    if unmapped_bgp:
        parts.append(
            (
                "BGP peer not mapped to inventory:",
                [f"{_I2}{x}" for x in sorted(set(unmapped_bgp))],
            )
        )
    if unmapped_isis:
        parts.append(
            (
                "Unmapped IS-IS adjacency:",
                [f"{_I2}{x}" for x in sorted(set(unmapped_isis))],
            )
        )
    if not parts:
        return []

    lines = ["Exceptions", "----------"]
    for idx, (title, items) in enumerate(parts):
        if idx:
            lines.append("")
        lines.append(f"{_I1}{title}")
        lines.extend(items)
    return lines


def _inventory_mismatch_lines(
    device: str,
    phys: list,
    mismatch_by_edge: dict[str, dict[str, Any]],
) -> list[str]:
    lines: list[str] = []
    for e in phys:
        if not isinstance(e, dict):
            continue
        if (e.get("local") or {}).get("device") != device:
            continue
        if _iface_pair_status(e) != "unknown":
            continue
        iface = str((e.get("local") or {}).get("interface") or "?")
        edge_id = str(e.get("id") or "")
        lines.append(
            _format_unknown_iface_line(iface, mismatch_by_edge.get(edge_id))
        )
    return sorted(lines)


def _interface_up_down_formatted(
    device: str,
    phys: list,
    troubleshoot: dict[str, Any] | None,
) -> list[str]:
    """One row per up/down iface: ``name: LLM cause`` (or not investigated)."""
    names = sorted(_iface_names_for_pair(device, phys, "up/down"))
    if not names:
        return []
    ts_iface = str((troubleshoot or {}).get("interface") or "")
    ts_summary = str((troubleshoot or {}).get("summary") or "").strip()
    out: list[str] = []
    for name in names:
        if name == ts_iface and ts_summary:
            cause = ts_summary
        else:
            cause = "(not investigated)"
        out.append(f"{_I2}{name}: {cause}")
    return out


def _interface_down_down_formatted(device: str, phys: list) -> list[str]:
    """Interface down/down names, four per row."""
    names = sorted(_iface_names_for_pair(device, phys, "down/down"))
    return _names_four_per_row(names, indent=_I2)


def _iface_names_for_pair(device: str, phys: list, pair: str) -> list[str]:
    names: list[str] = []
    for e in phys:
        if not isinstance(e, dict):
            continue
        if (e.get("local") or {}).get("device") != device:
            continue
        if _iface_pair_status(e) != pair:
            continue
        names.append(str((e.get("local") or {}).get("interface") or "?"))
    return names


def _names_four_per_row(
    names: list[str], *, indent: str = "  ", per_row: int = 4
) -> list[str]:
    if not names:
        return []
    width = max(len(name) for name in names)
    rows: list[str] = []
    for start in range(0, len(names), per_row):
        chunk = names[start : start + per_row]
        cells = [name.ljust(width) for name in chunk]
        rows.append(indent + "  ".join(cells).rstrip())
    return rows


def _troubleshooting_for_device(
    topology: Any, device: str
) -> dict[str, Any] | None:
    if not isinstance(topology, dict):
        return None
    operational = topology.get("operational")
    if not isinstance(operational, dict):
        return None
    blob = operational.get("interface_troubleshooting")
    if not isinstance(blob, dict):
        return None
    entry = blob.get(device)
    return entry if isinstance(entry, dict) else None


def _unexpected_live_by_device(topology: Any) -> dict[str, list[str]]:
    """Live interfaces not in static config, keyed by device (report-only)."""
    out: dict[str, list[str]] = {}
    for issue in operational_issues(topology):
        if issue.get("code") != "unexpected_live_object":
            continue
        layer = issue.get("layer")
        if layer is not None and layer != "physical":
            continue
        device, iface = _unexpected_live_device_iface(issue)
        if device and iface:
            bucket = out.setdefault(device, [])
            if iface not in bucket:
                bucket.append(iface)
    return out


def _unexpected_live_device_iface(issue: dict[str, Any]) -> tuple[str | None, str | None]:
    edge_id = issue.get("edge_id")
    if isinstance(edge_id, str) and edge_id.startswith("if:"):
        parts = edge_id.split(":", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            return parts[1], parts[2]
    message = str(issue.get("message") or "")
    # "lbnl-data-sw BV50000: live interface not present in static config"
    if ": live interface not present" in message:
        head = message.split(":", 1)[0].strip()
        bits = head.split(None, 1)
        if len(bits) == 2:
            return bits[0], bits[1]
    return None, None


def _admin_oper(edge: dict[str, Any]) -> tuple[str, str]:
    """Return (Status/Intf, Protocol/LineP) for an interface edge."""
    state = edge.get("state") or {}
    admin = state.get("admin")
    oper = state.get("oper")
    if admin is not None and oper is not None:
        return str(admin).lower(), str(oper).lower()
    st = edge_status(edge)
    if st == "up":
        return "up", "up"
    if st == "admin-down":
        return "admin-down", "admin-down"
    if st == "degraded":
        return "up", "up"
    if st == "unknown":
        return "unknown", "unknown"
    return "down", "down"


def _iface_pair_status(edge: dict[str, Any]) -> str:
    """Composite Status/Protocol label for Devices rollups.

    ``not-read``: the device's interfaces could not be read at all this run.
    ``no-state``: the device lists the interface but reports no state for it, so it is
    neither a mapping problem nor a down link.
    """
    if (edge.get("state") or {}).get("not_read"):
        return "not-read"
    if (edge.get("state") or {}).get("no_state"):
        return "no-state"
    admin, oper = _admin_oper(edge)
    if admin == "admin-down" or oper == "admin-down":
        return "admin-down"
    if admin == "unknown" or oper == "unknown":
        return "unknown"
    return f"{admin}/{oper}"


def _count_statuses(statuses: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {"total": len(statuses)}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    return counts


def _format_unknown_iface_line(
    iface: str,
    mismatch: dict[str, Any] | None,
) -> str:
    if not mismatch:
        return f"{iface} → (not on box)"
    confirmed = bool(mismatch.get("confirmed"))
    box = mismatch.get("box")
    candidates = mismatch.get("candidates") or []
    if confirmed and box:
        return f"{iface} → {compact_box_list(box)}  [confirmed]"
    if candidates:
        return f"{iface} → {compact_box_list(candidates)}  [suggested]"
    return f"{iface} → (not on box)"
