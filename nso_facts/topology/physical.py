"""Layer 0 — physical (configured interfaces + live oper state)."""

from __future__ import annotations

import re
from typing import Any

from nso_facts.mcp_client import call_mcp, unwrap_mcp_data
from nso_facts.topology.graph import (
    interface_edge_id,
    summarize_operational_physical,
    summarize_static_physical,
)
from nso_facts.topology.iface_equiv import (
    EquivalenceMap,
    compact_box_list,
    find_live_candidates,
    resolve_equivalence,
)
from nso_facts.topology.interfaces import interfaces_match

_INTERFACE_SUMMARY_LINE = re.compile(
    r"^\s*(\S+)\s+(up|down|admin-down)\s+(up|down|admin-down)",
    re.IGNORECASE,
)
# `show interfaces brief`: Name, Intf State, LineP State, then Encap…
_INTERFACE_BRIEF_LINE = re.compile(
    r"^\s*(\S+)\s+(up|down|admin-down)\s+(up|down|admin-down)\b",
    re.IGNORECASE,
)

_INTERFACE_CONFIG_SUFFIXES = (
    "tailf-ned-cisco-ios-xr:interface",
    "tailf-ned-cisco-ios:interface",
    "tailf-ned-cisco-ios-xe:interface",
    "openconfig-interfaces:interfaces",
)

_INTERFACE_LIST_KEYS = ("interface", "interfaces")


async def collect_static_physical(
    client: Any,
    device_names: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Discover configured interfaces from NSO CDB (static physical layer)."""
    edges: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    devices_queried = 0
    devices_failed = 0

    for device in device_names:
        try:
            device_edges = await _static_interfaces_for_device(client, device)
        except Exception as exc:  # noqa: BLE001 — continue other devices
            devices_failed += 1
            issues.append(
                {
                    "severity": "medium",
                    "layer": "physical",
                    "code": "collection_error",
                    "edge_id": None,
                    "message": f"{device}: static interface discovery failed: {exc}",
                }
            )
            continue

        devices_queried += 1
        if not device_edges:
            issues.append(
                {
                    "severity": "low",
                    "layer": "physical",
                    "code": "no_configured_interfaces",
                    "edge_id": None,
                    "message": f"{device}: no configured interfaces found in NSO CDB",
                }
            )
        edges.extend(device_edges)

    coverage = {
        "devices_total": len(device_names),
        "devices_queried": devices_queried,
        "devices_failed": devices_failed,
    }
    return edges, issues, coverage


async def collect_operational_physical(
    client: Any,
    static_edges: list[dict[str, Any]],
    equivalences: EquivalenceMap | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Refresh live interface state for configured physical edges."""
    static_by_device: dict[str, list[dict[str, Any]]] = {}
    for edge in static_edges:
        device = (edge.get("local") or {}).get("device")
        if device:
            static_by_device.setdefault(device, []).append(edge)

    operational_edges: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    devices_queried = 0
    devices_failed = 0
    device_names = sorted(static_by_device)
    eq_map = equivalences or {}

    for device in device_names:
        try:
            live, device_issues = await _operational_interfaces_for_device(
                client,
                device,
                static_by_device[device],
                equivalences=eq_map,
            )
        except Exception as exc:  # noqa: BLE001
            devices_failed += 1
            issues.append(
                {
                    "severity": "medium",
                    "layer": "physical",
                    "code": "collection_error",
                    "edge_id": None,
                    "message": f"{device}: interface health query failed: {exc}",
                }
            )
            for edge in static_by_device[device]:
                operational_edges.append(
                    _unknown_operational_edge(edge["id"], str(exc))
                )
            continue

        devices_queried += 1
        operational_edges.extend(live)
        issues.extend(device_issues)

    coverage = {
        "devices_total": len(device_names),
        "devices_queried": devices_queried,
        "devices_failed": devices_failed,
    }
    return operational_edges, issues, coverage


def build_static_physical_layer(edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": edges, "summary": summarize_static_physical(edges)}


def build_operational_physical_layer(
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    return {"edges": edges, "summary": summarize_operational_physical(edges)}


async def _static_interfaces_for_device(
    client: Any,
    device: str,
) -> list[dict[str, Any]]:
    suffixes = await _interface_config_suffixes(client, device)
    config_path = f"tailf-ncs:devices/device={device}/config"

    for suffix in suffixes:
        path = f"{config_path}/{suffix}"
        for depth in (3, 2):
            result = await call_mcp(
                client, "explore_nso_path", {"path": path, "depth": depth}
            )
            names = parse_configured_interface_names(result)
            if names:
                return [_static_interface_edge(device, name) for name in names]

    for depth in (3, 2):
        result = await call_mcp(
            client, "explore_nso_path", {"path": config_path, "depth": depth}
        )
        names = parse_configured_interface_names(result)
        if names:
            return [_static_interface_edge(device, name) for name in names]

    result = await call_mcp(client, "get_device_config", {"device_name": device})
    names = parse_configured_interface_names(result)
    return [_static_interface_edge(device, name) for name in names]


async def probe_static_interface_discovery(
    client: Any,
    device: str,
) -> dict[str, Any]:
    """Return per-path probe results for debugging empty physical edges."""
    config_path = f"tailf-ncs:devices/device={device}/config"
    suffixes = await _interface_config_suffixes(client, device)
    attempts: list[dict[str, Any]] = []

    for suffix in suffixes:
        path = f"{config_path}/{suffix}"
        for depth in (3, 2):
            result = await call_mcp(
                client, "explore_nso_path", {"path": path, "depth": depth}
            )
            names = parse_configured_interface_names(result)
            attempts.append(
                {
                    "tool": "explore_nso_path",
                    "path": path,
                    "depth": depth,
                    "interface_count": len(names),
                    "sample_interfaces": names[:5],
                    "error": _mcp_error(result),
                }
            )
            if names:
                return {
                    "device": device,
                    "discovered_suffixes": suffixes,
                    "selected": attempts[-1],
                    "attempts": attempts,
                }

    for depth in (3, 2):
        result = await call_mcp(
            client, "explore_nso_path", {"path": config_path, "depth": depth}
        )
        names = parse_configured_interface_names(result)
        attempts.append(
            {
                "tool": "explore_nso_path",
                "path": config_path,
                "depth": depth,
                "interface_count": len(names),
                "sample_interfaces": names[:5],
                "error": _mcp_error(result),
            }
        )
        if names:
            return {
                "device": device,
                "discovered_suffixes": suffixes,
                "selected": attempts[-1],
                "attempts": attempts,
            }

    result = await call_mcp(client, "get_device_config", {"device_name": device})
    names = parse_configured_interface_names(result)
    attempts.append(
        {
            "tool": "get_device_config",
            "path": config_path,
            "interface_count": len(names),
            "sample_interfaces": names[:5],
            "error": _mcp_error(result),
        }
    )
    return {
        "device": device,
        "discovered_suffixes": suffixes,
        "selected": attempts[-1] if attempts else None,
        "attempts": attempts,
    }


async def _interface_config_suffixes(client: Any, device: str) -> list[str]:
    """Discover YANG keys under device config that look like interface trees."""
    path = f"tailf-ncs:devices/device={device}/config"
    result = await call_mcp(client, "explore_nso_path", {"path": path, "depth": 1})
    data = unwrap_mcp_data(result)
    discovered: list[str] = []
    if isinstance(data, dict):
        for key in data:
            key_str = str(key)
            if "interface" in key_str.lower():
                discovered.append(key_str)

    combined: list[str] = []
    for suffix in discovered + list(_INTERFACE_CONFIG_SUFFIXES):
        if suffix not in combined:
            combined.append(suffix)
    return combined


def _mcp_error(result: Any) -> str | None:
    if isinstance(result, dict) and result.get("status") == "error":
        return str(result.get("error_message") or "unknown error")
    data = unwrap_mcp_data(result)
    if isinstance(data, dict) and data.get("truncated"):
        return str(data.get("hint") or "response truncated")
    return None


async def _operational_interfaces_for_device(
    client: Any,
    device: str,
    static_edges: list[dict[str, Any]],
    equivalences: EquivalenceMap | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result = await call_mcp(
        client,
        "exec_show",
        {"device_name": device, "input_command": "interfaces brief"},
    )
    issues: list[dict[str, Any]] = []
    eq_map = equivalences or {}

    if isinstance(result, dict) and result.get("status") == "error":
        message = result.get("error_message", "exec_show interfaces brief failed")
        return (
            [_unknown_operational_edge(edge["id"], message) for edge in static_edges],
            [
                {
                    "severity": "medium",
                    "layer": "physical",
                    "code": "collection_error",
                    "edge_id": None,
                    "message": f"{device}: {message}",
                }
            ],
        )

    data = unwrap_mcp_data(result)
    brief_text = ""
    if isinstance(data, dict):
        brief_text = str(data.get("result") or "")
    live = parse_interfaces_brief(brief_text)

    static_entries: list[tuple[str, str]] = []
    for edge in static_edges:
        iface = (edge.get("local") or {}).get("interface")
        edge_id = edge.get("id")
        if isinstance(iface, str) and isinstance(edge_id, str):
            static_entries.append((iface, edge_id))

    static_names = {iface for iface, _ in static_entries}
    for nso_name, box_names in (eq_map.get(device) or {}).items():
        if nso_name not in static_names:
            issues.append(
                {
                    "severity": "low",
                    "layer": "physical",
                    "code": "equivalence_ignored",
                    "edge_id": None,
                    "message": (
                        f"{device}: equivalence for {nso_name} ignored "
                        f"(not in static config); box={box_names}"
                    ),
                }
            )

    operational: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    matched_live: set[str] = set()

    for static_iface, edge_id in static_entries:
        live_name = _find_matching_live_iface(static_iface, live)
        if live_name is None:
            continue
        admin_oper = live[live_name]
        operational.append(
            _operational_edge(
                edge_id,
                admin_oper["admin"],
                admin_oper["oper"],
                has_errors=False,
            )
        )
        seen_ids.add(edge_id)
        matched_live.add(live_name)

    for static_iface, edge_id in static_entries:
        if edge_id in seen_ids:
            continue
        box = resolve_equivalence(eq_map, device, static_iface)
        if box is not None:
            present = [name for name in box if name in live]
            if present:
                operational.append(
                    _worst_operational_edge(edge_id, present, live)
                )
                seen_ids.add(edge_id)
                matched_live.update(present)
                continue
            issues.append(
                _mismatch_issue(
                    device=device,
                    edge_id=edge_id,
                    nso=static_iface,
                    candidates=[],
                    kind="missing",
                    confirmed=True,
                    box=box,
                    message_suffix="admin box name(s) missing from brief",
                )
            )
            operational.append(
                _unknown_operational_edge(
                    edge_id,
                    "admin equivalence box not in interfaces brief",
                )
            )
            seen_ids.add(edge_id)
            continue

        available = [name for name in live if name not in matched_live]
        candidates, kind = find_live_candidates(static_iface, available)
        issues.append(
            _mismatch_issue(
                device=device,
                edge_id=edge_id,
                nso=static_iface,
                candidates=candidates,
                kind=kind,
                confirmed=False,
                box=None,
            )
        )
        operational.append(
            _unknown_operational_edge(edge_id, "not in interfaces brief")
        )
        seen_ids.add(edge_id)

    for iface in live:
        if iface in matched_live:
            continue
        issues.append(
            {
                "severity": "low",
                "layer": "physical",
                "code": "unexpected_live_object",
                "edge_id": interface_edge_id(device, iface),
                "message": (
                    f"{device} {iface}: live interface not present in static config"
                ),
            }
        )

    return operational, issues


def _mismatch_issue(
    *,
    device: str,
    edge_id: str,
    nso: str,
    candidates: list[str],
    kind: str,
    confirmed: bool,
    box: list[str] | None,
    message_suffix: str | None = None,
) -> dict[str, Any]:
    if message_suffix:
        detail = message_suffix
    elif candidates:
        detail = f"candidates: {compact_box_list(candidates)}"
    else:
        detail = "not in interfaces brief"
    return {
        "severity": "low",
        "layer": "physical",
        "code": "config_live_mismatch",
        "edge_id": edge_id,
        "message": f"{device} {nso}: {detail}",
        "device": device,
        "nso": nso,
        "candidates": candidates,
        "kind": kind,
        "confirmed": confirmed,
        "box": box,
    }


def _worst_operational_edge(
    edge_id: str,
    live_names: list[str],
    live: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Pick the worst admin/oper among listed live interfaces."""
    rank = {
        "admin-down": 0,
        "down": 1,
        "unknown": 2,
        "degraded": 3,
        "up": 4,
    }
    best_name = live_names[0]
    best_rank = 99
    for name in live_names:
        admin_oper = live[name]
        status = _classify_interface_status(
            admin_oper["admin"], admin_oper["oper"], has_errors=False
        )
        r = rank.get(status, 2)
        if r < best_rank:
            best_rank = r
            best_name = name
    admin_oper = live[best_name]
    return _operational_edge(
        edge_id,
        admin_oper["admin"],
        admin_oper["oper"],
        has_errors=False,
    )


def _find_matching_live_iface(
    static_iface: str,
    live: dict[str, dict[str, str]],
) -> str | None:
    for live_name in live:
        if interfaces_match(static_iface, live_name):
            return live_name
    return None


def parse_configured_interface_names(explore_result: Any) -> list[str]:
    """Extract interface names from explore_nso_path config payloads."""
    if isinstance(explore_result, dict) and explore_result.get("status") == "error":
        return []

    data = unwrap_mcp_data(explore_result)
    if isinstance(data, dict) and data.get("truncated"):
        return []

    names: list[str] = []
    _walk_for_interface_names(data, names)
    return sorted(set(names))


def parse_interfaces_summary(text: str) -> dict[str, dict[str, str]]:
    """Parse legacy per-row `show interfaces summary` text into {iface: {admin, oper}}."""
    live: dict[str, dict[str, str]] = {}
    if not text:
        return live

    for line in text.splitlines():
        match = _INTERFACE_SUMMARY_LINE.match(line)
        if not match:
            continue
        iface, admin, oper = match.group(1), match.group(2), match.group(3)
        live[iface] = {"admin": admin.lower(), "oper": oper.lower()}
    return live


def parse_interfaces_brief(text: str) -> dict[str, dict[str, str]]:
    """Parse `show interfaces brief` into {iface: {admin, oper}}."""
    live: dict[str, dict[str, str]] = {}
    if not text:
        return live

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("---") or "Intf" in stripped and "Name" in stripped:
            continue
        if stripped.lower().startswith("interface type"):
            continue
        match = _INTERFACE_BRIEF_LINE.match(line)
        if not match:
            continue
        iface, admin, oper = match.group(1), match.group(2), match.group(3)
        live[iface] = {"admin": admin.lower(), "oper": oper.lower()}
    return live


def _static_interface_edge(device: str, interface: str) -> dict[str, Any]:
    return {
        "id": interface_edge_id(device, interface),
        "type": "interface",
        "local": {"device": device, "interface": interface},
        "remote": None,
    }


def _operational_edge(
    edge_id: str,
    admin: str,
    oper: str,
    has_errors: bool,
) -> dict[str, Any]:
    status = _classify_interface_status(admin, oper, has_errors)
    return {
        "id": edge_id,
        "state": {
            "admin": admin,
            "oper": oper,
            "status": status,
            "has_errors": has_errors,
        },
    }


def _unknown_operational_edge(edge_id: str, detail: str) -> dict[str, Any]:
    return {
        "id": edge_id,
        "state": {"status": "unknown", "detail": detail},
    }


def _classify_interface_status(admin: str, oper: str, has_errors: bool) -> str:
    """Map brief Intf/LineP (and optional errors) to a report status.

    Taxonomy:
    - ``up`` — admin up and line protocol up (no error flag)
    - ``admin-down`` — administratively shut
    - ``down`` — not admin-down, but not fully up (e.g. admin up / line down)
    - ``degraded`` — admin+oper up with error counters (unused when brief-only)
    - ``unknown`` — set separately when the interface is missing from brief
    """
    if admin == "admin-down":
        return "admin-down"
    if admin == "up" and oper == "up":
        return "degraded" if has_errors else "up"
    if oper != "up" or admin == "down":
        return "down"
    return "degraded"


def _interfaces_with_errors(error_lines: list[Any]) -> set[str]:
    names: set[str] = set()
    for line in error_lines:
        text = str(line).strip()
        if not text:
            continue
        names.add(text.split()[0])
    return names


def _walk_for_interface_names(obj: Any, names: list[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_str = str(key)
            if key_str.endswith(":interface") or key_str == "interface":
                _collect_interface_list(value, names)
            elif key_str.endswith(":interfaces") or key_str == "interfaces":
                _collect_interface_list(value, names)
            elif key in _INTERFACE_LIST_KEYS:
                _collect_interface_list(value, names)
            elif isinstance(value, (dict, list)):
                _walk_for_interface_names(value, names)
    elif isinstance(obj, list):
        for item in obj:
            _walk_for_interface_names(item, names)


def _collect_interface_list(value: Any, names: list[str]) -> None:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
            elif isinstance(item, str) and item.strip():
                names.append(item.strip())
        return

    if isinstance(value, dict):
        for key in _INTERFACE_LIST_KEYS:
            if key in value:
                _collect_interface_list(value[key], names)
                return
        name = value.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
