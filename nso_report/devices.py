"""Per-device topology rollups for Detailed Device Analysis."""

from __future__ import annotations

from collections import Counter
from typing import Any

from nso_facts.fleet_rollups import (
    devices_in_edges,
    edge_status,
    merged_edges,
    mismatch_by_edge_id,
    operational_issues,
    operational_layers,
    peer_up_total,
    static_layers,
    unmapped_by_device,
)
from nso_facts.health import build_device_sync_map
from nso_facts.topology.iface_equiv import (
    compact_box_list,
    suggested_equivalences_report_note,
)

INVENTORY_REVIEW_STATUS = "Inventory Review"

_BANNER = "====================================================="
_I1 = "  "
_I2 = "    "
_I3 = "      "


def format_devices_section(
    topology: Any,
    *,
    services: dict[str, Any] | None = None,
    fleet_sync: Any = None,
    hardware_health: dict[str, Any] | None = None,
    extra_devices: list[str] | None = None,
    only_devices: list[str] | None = None,
) -> str:
    """Build Detailed Device Analysis body from topology (+ optional services)."""
    op_layers = operational_layers(topology)
    if op_layers is None:
        return "No device topology in snapshot."

    static = static_layers(topology)
    phys = merged_edges(op_layers, static, "physical")
    under = merged_edges(op_layers, static, "underlay")
    route = merged_edges(op_layers, static, "routing")
    unmapped_bgp, unmapped_isis = unmapped_by_device(topology)
    unexpected_live = _unexpected_live_by_device(topology)
    mismatch_by_edge = mismatch_by_edge_id(topology)
    sync_map = build_device_sync_map(fleet_sync)
    services_by_device = _index_services_by_device(services)
    hw = hardware_health if isinstance(hardware_health, dict) else {}
    named = {
        n.strip()
        for n in (extra_devices or [])
        if isinstance(n, str) and n.strip()
    }

    devices = sorted(
        devices_in_edges(phys, under, route)
        | set(unmapped_bgp)
        | set(unmapped_isis)
        | set(unexpected_live)
        | set(services_by_device)
        | set(hw)
        | named
    )
    if only_devices:
        allow = {n.strip() for n in only_devices if isinstance(n, str) and n.strip()}
        devices = [d for d in devices if d in allow]
        # Ensure requested focus devices still appear even with no edges yet
        for name in sorted(allow):
            if name not in devices:
                devices.append(name)
        devices = sorted(devices)
    if not devices:
        return "No device topology in snapshot."

    blocks: list[str] = []
    for device in devices:
        blocks.append(
            "\n".join(
                _format_one_device(
                    device,
                    phys=phys,
                    under=under,
                    route=route,
                    unmapped_bgp=unmapped_bgp.get(device, []),
                    unmapped_isis=unmapped_isis.get(device, []),
                    unexpected_live=unexpected_live.get(device, []),
                    mismatch_by_edge=mismatch_by_edge,
                    sync_result=sync_map.get(device),
                    service_records=services_by_device.get(device, []),
                    troubleshoot=_troubleshooting_for_device(topology, device),
                    hardware_entry=hw.get(device),
                )
            )
        )
    body = "\n\n".join(blocks)
    suggested_n = _suggested_mismatch_count(mismatch_by_edge)
    if suggested_n:
        body = f"{suggested_equivalences_report_note(suggested_n)}\n\n{body}"
    return body


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


def _index_services_by_device(
    services: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(services, dict):
        return out
    for record in services.values():
        if not isinstance(record, dict):
            continue
        for device in record.get("devices") or []:
            if not isinstance(device, str) or not device.strip():
                continue
            out.setdefault(device.strip(), []).append(record)
    return out


def _format_one_device(
    device: str,
    *,
    phys: list,
    under: list,
    route: list,
    unmapped_bgp: list[str],
    unmapped_isis: list[str],
    unexpected_live: list[str],
    mismatch_by_edge: dict[str, dict[str, Any]],
    sync_result: str | None,
    service_records: list[dict[str, Any]],
    troubleshoot: dict[str, Any] | None,
    hardware_entry: dict[str, Any] | None = None,
) -> list[str]:
    from agent.hardware_health import format_device_hardware_section

    lines = [
        _BANNER,
        f"Device: {device}",
        _BANNER,
        "",
    ]
    lines.extend(
        _status_section(
            device,
            phys=phys,
            under=under,
            route=route,
            mismatch_by_edge=mismatch_by_edge,
            sync_result=sync_result,
            service_records=service_records,
        )
    )
    lines.append("")
    lines.extend(_routing_section(device, route, under))
    lines.append("")
    lines.extend(_services_section(service_records))
    lines.append("")
    lines.extend(_interfaces_summary_section(device, phys))
    lines.append("")
    lines.extend(format_device_hardware_section(hardware_entry))
    exceptions = _exceptions_section(
        device,
        phys=phys,
        mismatch_by_edge=mismatch_by_edge,
        unmapped_bgp=unmapped_bgp,
        unmapped_isis=unmapped_isis,
        unexpected_live=unexpected_live,
        troubleshoot=troubleshoot,
    )
    if exceptions:
        lines.append("")
        lines.extend(exceptions)
    return lines


def _status_section(
    device: str,
    *,
    phys: list,
    under: list,
    route: list,
    mismatch_by_edge: dict[str, dict[str, Any]],
    sync_result: str | None,
    service_records: list[dict[str, Any]],
) -> list[str]:
    overall = _device_overall(device, phys, mismatch_by_edge)
    reasons = _device_reason_bullets(
        device,
        phys=phys,
        under=under,
        route=route,
        mismatch_by_edge=mismatch_by_edge,
        sync_result=sync_result,
        service_records=service_records,
        overall=overall,
    )
    lines = [
        "Status",
        "------",
        f"{_I1}Overall: {overall}",
        "",
        f"{_I1}Reason:",
    ]
    if reasons:
        lines.extend(f"{_I2}- {r}" for r in reasons)
    else:
        lines.append(f"{_I2}- No material issues noted")
    return lines


def _device_overall(
    device: str,
    phys: list,
    mismatch_by_edge: dict[str, dict[str, Any]],
) -> str:
    for e in phys:
        if not isinstance(e, dict):
            continue
        if (e.get("local") or {}).get("device") != device:
            continue
        if _iface_pair_status(e) in {"up/down", "unknown"}:
            return INVENTORY_REVIEW_STATUS
    for issue in mismatch_by_edge.values():
        if issue.get("confirmed"):
            continue
        if issue.get("device") == device:
            return INVENTORY_REVIEW_STATUS
    return "Healthy"


def _device_reason_bullets(
    device: str,
    *,
    phys: list,
    under: list,
    route: list,
    mismatch_by_edge: dict[str, dict[str, Any]],
    sync_result: str | None,
    service_records: list[dict[str, Any]],
    overall: str,
) -> list[str]:
    bullets: list[str] = []
    if sync_result == "in-sync":
        bullets.append("Device is synchronized with NSO")
    elif sync_result:
        bullets.append(f"Device sync: {sync_result}")
    elif sync_result is None:
        pass

    if service_records:
        bad = [
            r
            for r in service_records
            if str(r.get("status") or "").lower() not in {"up", ""}
        ]
        if not bad:
            bullets.append("Services are operational")
        else:
            bullets.append(
                f"{len(bad)} service instance(s) not fully up"
            )

    bgp_up, bgp_total = peer_up_total(device, route)
    isis_up, isis_total = peer_up_total(device, under)
    routing_bits: list[str] = []
    if bgp_total and bgp_up == bgp_total:
        routing_bits.append("BGP")
    if isis_total and isis_up == isis_total:
        routing_bits.append("IS-IS")
    if routing_bits and (not bgp_total or bgp_up == bgp_total) and (
        not isis_total or isis_up == isis_total
    ):
        if len(routing_bits) == 2:
            bullets.append("BGP and IS-IS are healthy")
        elif "BGP" in routing_bits:
            bullets.append("BGP is healthy")
        else:
            bullets.append("IS-IS is healthy")
    elif bgp_total and bgp_up < bgp_total:
        bullets.append(f"BGP peers {bgp_up}/{bgp_total} Established")
    elif isis_total and isis_up < isis_total:
        bullets.append(f"IS-IS adjacencies {isis_up}/{isis_total} Up")

    mismatch_n = sum(
        1
        for issue in mismatch_by_edge.values()
        if not issue.get("confirmed") and issue.get("device") == device
    )
    if mismatch_n:
        noun = "mismatch" if mismatch_n == 1 else "mismatches"
        verb = "requires" if mismatch_n == 1 else "require"
        bullets.append(
            f"{mismatch_n} interface inventory {noun} {verb} review"
        )

    up_down = _iface_names_for_pair(device, phys, "up/down")
    if up_down:
        bullets.append(f"{len(up_down)} interface(s) up/down")

    if overall == "Healthy" and not bullets:
        bullets.append("No material issues noted")
    return bullets


def _routing_section(device: str, route: list, under: list) -> list[str]:
    lines = ["Routing", "-------", f"{_I1}BGP"]
    peers = _bgp_peers_for_device(device, route)
    established = sum(1 for _, st in peers if st == "up")
    down = len(peers) - established
    lines.append(f"{_I2}Peers: {len(peers)}")
    lines.append(f"{_I2}Established: {established}")
    lines.append(f"{_I2}Down: {down}")
    if peers:
        lines.append("")
        lines.append(f"{_I2}Peer                State")
        lines.append(f"{_I2}-------------------------")
        for peer, st in peers:
            state = "Established" if st == "up" else st.capitalize()
            lines.append(f"{_I2}{peer:<20}{state}")

    lines.append("")
    lines.append(f"{_I1}IS-IS")
    adj = _isis_neighbors_for_device(device, under)
    up_n = sum(1 for _, st in adj if st == "up")
    down_n = len(adj) - up_n
    lines.append(f"{_I2}Adjacencies: {len(adj)}")
    lines.append(f"{_I2}Up: {up_n}")
    lines.append(f"{_I2}Down: {down_n}")
    if adj:
        lines.append("")
        lines.append(f"{_I2}Neighbors:")
        for neighbor, st in adj:
            state = "Up" if st == "up" else st.capitalize()
            lines.append(f"{_I3}{neighbor}  {state}")
    return lines


def _bgp_peers_for_device(device: str, route: list) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for e in route:
        if not isinstance(e, dict):
            continue
        local = e.get("local") or {}
        remote = e.get("remote") or {}
        if device == local.get("device"):
            peer = str(remote.get("address") or remote.get("device") or "?")
        elif device == remote.get("device"):
            peer = str(local.get("address") or local.get("device") or "?")
        else:
            continue
        if peer in seen:
            continue
        seen.add(peer)
        rows.append((peer, edge_status(e)))
    return sorted(rows, key=lambda r: r[0])


def _isis_neighbors_for_device(
    device: str, under: list
) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for e in under:
        if not isinstance(e, dict):
            continue
        local = e.get("local") or {}
        remote = e.get("remote") or {}
        if device == local.get("device"):
            peer = str(remote.get("device") or remote.get("interface") or "?")
        elif device == remote.get("device"):
            peer = str(local.get("device") or local.get("interface") or "?")
        else:
            continue
        if peer in seen:
            continue
        seen.add(peer)
        rows.append((peer, edge_status(e)))
    return sorted(rows, key=lambda r: r[0])


def _services_section(service_records: list[dict[str, Any]]) -> list[str]:
    lines = ["Services", "--------"]
    if not service_records:
        lines.append(f"{_I1}Deployed Services: 0")
        return lines

    by_type: dict[str, Counter[str]] = {}
    for rec in service_records:
        stype = str(rec.get("service_type") or "unknown")
        status = str(rec.get("status") or "unknown").lower()
        bucket = by_type.setdefault(stype, Counter())
        bucket["total"] += 1
        if status == "up":
            bucket["up"] += 1
        else:
            bucket["down"] += 1

    lines.append(f"{_I1}Deployed Services: {len(service_records)}")
    lines.append("")
    lines.append(f"{_I1}Type          Total   Up   Down")
    lines.append(f"{_I1}--------------------------------")
    for stype in sorted(by_type):
        c = by_type[stype]
        lines.append(
            f"{_I1}{stype:<14}{c['total']:>5}{c['up']:>5}{c['down']:>7}"
        )
    return lines


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


def _suggested_mismatch_count(
    mismatch_by_edge: dict[str, dict[str, Any]],
) -> int:
    return sum(1 for issue in mismatch_by_edge.values() if not issue.get("confirmed"))


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
