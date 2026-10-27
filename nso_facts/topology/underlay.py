"""Layer 1 — underlay (IS-IS adjacencies from config + live state)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nso_facts.mcp_client import call_mcp, unwrap_mcp_data
from nso_facts.topology.graph import (
    isis_edge_id,
    ordered_isis_endpoints,
    summarize_operational_underlay,
    summarize_static_underlay,
)
from nso_facts.topology.interfaces import (
    canonical_interface_name,
    interfaces_match,
    physical_interfaces_by_device,
)

_ISIS_CONFIG_SUFFIXES = (
    "tailf-ned-cisco-ios-xr:router/isis",
    "tailf-ned-cisco-ios-xr:router",
    "tailf-ned-cisco-ios:router/isis",
)

_ISIS_INTERFACE_KEYS = ("interface", "interfaces")


@dataclass(frozen=True)
class AdjacencyObservation:
    local_device: str
    local_interface: str
    neighbor_system_id: str
    state: str


async def collect_static_underlay(
    client: Any,
    device_names: list[str],
    *,
    seed_from_live: bool = True,
    physical_edges: list[dict[str, Any]] | None = None,
    expected_edges: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Build static IS-IS adjacency edges for topology.static.json.

    IS-IS config in IOS-XR usually lists local interfaces only, not remote device
    names. On static rebuild we optionally pair adjacencies using one live
    `check_isis_adjacencies` pass (`seed_from_live`) and validate against
    config-enabled IS-IS interfaces. ``expected_edges`` (from an earlier scan)
    adds the adjacencies this pass no longer shows, when its answers and the
    config still call for them.
    """
    issues: list[dict[str, Any]] = []
    isis_interfaces: dict[str, set[str]] = {}
    devices_queried = 0
    devices_failed = 0

    for device in device_names:
        try:
            names = await _static_isis_interfaces_for_device(client, device)
            isis_interfaces[device] = set(names)
            devices_queried += 1
            if not names:
                issues.append(
                    {
                        "severity": "low",
                        "layer": "underlay",
                        "code": "no_configured_isis",
                        "edge_id": None,
                        "message": f"{device}: no IS-IS interfaces found in NSO config",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            devices_failed += 1
            isis_interfaces[device] = set()
            issues.append(
                {
                    "severity": "medium",
                    "layer": "underlay",
                    "code": "collection_error",
                    "edge_id": None,
                    "message": f"{device}: static IS-IS discovery failed: {exc}",
                }
            )

    physical_by_device = physical_interfaces_by_device(physical_edges or [])
    edges: list[dict[str, Any]] = []
    if seed_from_live and device_names:
        observations, live_issues, _ = await _collect_observations(client, device_names)
        issues.extend(live_issues)
        paired, pair_issues = pair_adjacency_observations(
            observations,
            device_names,
            physical_by_device=physical_by_device,
            unread_devices=_unread_devices(live_issues),
            known_edges=expected_edges,
        )
        issues.extend(pair_issues)
        for edge in paired:
            if _edge_allowed_by_config(edge, isis_interfaces):
                edges.append(_static_adjacency_edge(edge))
            else:
                issues.append(
                    {
                        "severity": "low",
                        "layer": "underlay",
                        "code": "live_adjacency_not_in_config",
                        "edge_id": edge["id"],
                        "message": (
                            f"Live IS-IS adjacency {edge['id']} not fully reflected "
                            "in IS-IS interface config"
                        ),
                    }
                )
        edges.extend(_missing_expected_edges(
            expected_edges or [], paired, device_names=device_names,
            unread=_unread_devices(live_issues), isis_interfaces=isis_interfaces))

    coverage = {
        "devices_total": len(device_names),
        "devices_queried": devices_queried,
        "devices_failed": devices_failed,
    }
    return edges, issues, coverage


async def collect_operational_underlay(
    client: Any,
    device_names: list[str],
    static_edges: list[dict[str, Any]],
    *,
    physical_edges: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Refresh live IS-IS adjacency state and compare to static edge set."""
    physical_by_device = physical_interfaces_by_device(physical_edges or [])
    observations, issues, coverage = await _collect_observations(client, device_names)
    paired, pair_issues = pair_adjacency_observations(
        observations,
        device_names,
        physical_by_device=physical_by_device,
        unread_devices=_unread_devices(issues),
        known_edges=static_edges,
    )
    issues.extend(pair_issues)

    static_by_id = {edge["id"]: edge for edge in static_edges}
    operational: list[dict[str, Any]] = []

    for edge in paired:
        operational.append(
            {
                "id": edge["id"],
                "state": edge["state"],
            }
        )

    paired_ids = {edge["id"] for edge in paired}
    unread_now = _unread_devices(issues)
    for static_id, static_edge in static_by_id.items():
        # A one-sided edge only stood for a far end that could not be read.
        if static_id in paired_ids or _is_one_sided(static_edge):
            continue
        local = static_edge.get("local") or {}
        remote = static_edge.get("remote") or {}
        ends = [device for device in (local.get("device"), remote.get("device")) if device]
        # A router that did not answer this pass proves nothing about the adjacency.
        if unread_now & set(ends):
            continue
        operational.append(
            {
                "id": static_id,
                "state": {
                    "local": "unknown",
                    "remote": "unknown",
                    "status": "down",
                    "detail": MISSING_DETAIL,
                },
            }
        )
        why = _EXPECTED_WHY.get((static_edge.get("meta") or {}).get("expected"), "seen up earlier in this scan")
        issues.append(
            {
                "severity": "high",
                "layer": "underlay",
                "code": "configured_no_adjacency",
                "edge_id": static_id,
                "devices": ends,
                "message": (
                    f"IS-IS adjacency down: "
                    f"{local.get('device')} {local.get('interface')} ↔ "
                    f"{remote.get('device')} {remote.get('interface')} "
                    f"(both routers answered without it; {why})"
                ),
            }
        )

    for edge in paired:
        if edge["id"] not in static_by_id:
            issues.append(
                {
                    "severity": "low",
                    "layer": "underlay",
                    "code": "unexpected_live_object",
                    "edge_id": edge["id"],
                    "message": f"Live IS-IS adjacency not in static topology: {edge['id']}",
                }
            )

    return operational, issues, coverage


def build_static_underlay_layer(edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": edges, "summary": summarize_static_underlay(edges)}


def build_operational_underlay_layer(edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": edges, "summary": summarize_operational_underlay(edges)}


def pair_adjacency_observations(
    observations: list[AdjacencyObservation],
    device_names: list[str],
    *,
    physical_by_device: dict[str, set[str]] | None = None,
    unread_devices: set[str] | frozenset[str] = frozenset(),
    known_edges: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair one-way IS-IS observations into bidirectional underlay edges.

    An adjacency toward a device whose own IS-IS could not be read is kept
    one-sided: this end's reading, the far end ``unknown``, and the far
    interface written ``?`` because only that device could name it. One the
    far device answered without is kept too, its end ``not listed``; its
    interface then comes from ``known_edges`` (such as the previous scan's).
    """
    issues: list[dict[str, Any]] = []
    by_device: dict[str, list[AdjacencyObservation]] = {}
    for obs in observations:
        by_device.setdefault(obs.local_device, []).append(obs)

    used_remote: set[tuple[str, str]] = set()
    edges: list[dict[str, Any]] = []

    for obs_a in observations:
        remote_device = resolve_system_id(obs_a.neighbor_system_id, device_names)
        if remote_device is None:
            issues.append(
                {
                    "severity": "medium",
                    "layer": "underlay",
                    "code": "unknown_neighbor_system_id",
                    "edge_id": None,
                    "message": (
                        f"{obs_a.local_device} {obs_a.local_interface}: "
                        f"unmapped IS-IS system id {obs_a.neighbor_system_id!r}"
                    ),
                }
            )
            continue
        if remote_device == obs_a.local_device:
            continue

        obs_b = _find_reverse_observation(
            by_device.get(remote_device, []),
            local_device=remote_device,
            remote_device=obs_a.local_device,
            used_remote=used_remote,
            device_names=device_names,
        )
        if obs_b is None:
            if remote_device in unread_devices:
                edge = _one_sided_edge(obs_a, remote_device, physical_by_device)
                if not any(existing["id"] == edge["id"] for existing in edges):
                    edges.append(edge)
            else:
                # The far device answered without it: its silence is a reading too.
                edge = _half_listed_edge(obs_a, remote_device, physical_by_device, known_edges or [])
                if not any(existing["id"] == edge["id"] for existing in edges):
                    edges.append(edge)
                    if edge["state"]["status"] != "up":
                        issues.append(_views_issue(edge))
            continue

        if_a = canonical_interface_name(
            obs_a.local_device, obs_a.local_interface, physical_by_device
        )
        if_b = canonical_interface_name(
            obs_b.local_device, obs_b.local_interface, physical_by_device
        )
        (dev_a, if_a), (dev_b, if_b) = ordered_isis_endpoints(
            obs_a.local_device,
            if_a,
            obs_b.local_device,
            if_b,
        )
        edge_id = isis_edge_id(dev_a, if_a, dev_b, if_b)
        if any(existing["id"] == edge_id for existing in edges):
            continue

        # Each state stays with its own device once the ends are ordered.
        states = {
            obs_a.local_device: _normalize_isis_state(obs_a.state),
            obs_b.local_device: _normalize_isis_state(obs_b.state),
        }
        state_a, state_b = states[dev_a], states[dev_b]
        status = _bidirectional_status(state_a, state_b)
        edges.append(
            {
                "id": edge_id,
                "type": "isis_adjacency",
                "local": {"device": dev_a, "interface": if_a},
                "remote": {"device": dev_b, "interface": if_b},
                "state": {
                    "local": state_a,
                    "remote": state_b,
                    "status": status,
                },
            }
        )
        used_remote.add((remote_device, obs_b.local_interface))

        if status == "unidirectional":
            issues.append(_views_issue(edges[-1]))

    return edges, issues


def resolve_system_id(system_id: str, device_names: list[str]) -> str | None:
    sid = system_id.strip().lower()
    if not sid:
        return None
    lowered = {name.lower(): name for name in device_names}
    if sid in lowered:
        return lowered[sid]
    for name_lower, name in lowered.items():
        if sid == name_lower.replace("-", "") or name_lower in sid or sid in name_lower:
            return name
    return None


async def probe_static_isis_discovery(
    client: Any,
    device: str,
) -> dict[str, Any]:
    """Debug IS-IS config path discovery for one device."""
    config_path = f"tailf-ncs:devices/device={device}/config"
    suffixes = await _isis_config_suffixes(client, device)
    attempts: list[dict[str, Any]] = []

    for suffix in suffixes:
        path = f"{config_path}/{suffix}"
        for depth in (3, 2):
            result = await call_mcp(
                client, "explore_nso_path", {"path": path, "depth": depth}
            )
            names = parse_configured_isis_interfaces(result)
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
        names = parse_configured_isis_interfaces(result)
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
    names = parse_configured_isis_interfaces(result)
    attempts.append(
        {
            "tool": "get_device_config",
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


# A NED that models IS-IS config only (Ciena SAOS): the MCP server's code, and our wording for it.
_NOT_EXPOSED_CODE = "not_exposed"
NOT_EXPOSED = "IS-IS adjacency state is not exposed on this platform"


class AdjacencyStateNotExposed(RuntimeError):
    """The device's NED has no IS-IS adjacency state to read; retrying cannot help."""


def isis_state_not_exposed(issue: dict[str, Any]) -> bool:
    """An IS-IS end that is unread because of the platform, not because a read failed."""
    return (issue.get("code") == "collection_error" and issue.get("layer") == "underlay"
            and NOT_EXPOSED in str(issue.get("message") or ""))


async def _collect_observations(
    client: Any,
    device_names: list[str],
) -> tuple[list[AdjacencyObservation], list[dict[str, Any]], dict[str, int]]:
    observations: list[AdjacencyObservation] = []
    issues: list[dict[str, Any]] = []
    devices_queried = 0
    devices_failed = 0

    for device in device_names:
        try:
            observations.extend(await _observations_for_device(client, device))
            devices_queried += 1
        except AdjacencyStateNotExposed as exc:
            # Still an unread end, so the neighbor's view stands for the adjacency.
            devices_failed += 1
            issues.append(
                {
                    "severity": "low",
                    "layer": "underlay",
                    "code": "collection_error",
                    "edge_id": None,
                    "message": f"{device}: {NOT_EXPOSED} ({exc})",
                }
            )
        except Exception as exc:  # noqa: BLE001
            devices_failed += 1
            issues.append(
                {
                    "severity": "medium",
                    "layer": "underlay",
                    "code": "collection_error",
                    "edge_id": None,
                    "message": f"{device}: check_isis_adjacencies failed: {exc}",
                }
            )

    coverage = {
        "devices_total": len(device_names),
        "devices_queried": devices_queried,
        "devices_failed": devices_failed,
    }
    return observations, issues, coverage


async def _observations_for_device(
    client: Any,
    device: str,
) -> list[AdjacencyObservation]:
    result = await call_mcp(
        client, "check_isis_adjacencies", {"device_name": device}
    )
    if isinstance(result, dict) and result.get("status") == "error":
        if result.get("error_code") == _NOT_EXPOSED_CODE:
            raise AdjacencyStateNotExposed(result.get("error_message", ""))
        raise RuntimeError(result.get("error_message", "check_isis_adjacencies failed"))

    data = unwrap_mcp_data(result)
    rows = data.get("adjacencies") or []
    observations: list[AdjacencyObservation] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        system_id = str(row.get("system_id") or "").strip()
        interface = str(row.get("interface") or "").strip()
        state = str(row.get("state") or "unknown").strip()
        if system_id and interface:
            observations.append(
                AdjacencyObservation(device, interface, system_id, state)
            )
    return observations


async def _static_isis_interfaces_for_device(
    client: Any,
    device: str,
) -> list[str]:
    """Discover IS-IS interfaces from NSO intent for one device.

    Prefer ``get_device_config``; fall back to the explore_nso_path NED
    ladder only when full config yields no IS-IS interfaces.
    """
    result = await call_mcp(client, "get_device_config", {"device_name": device})
    names = parse_configured_isis_interfaces(result)
    if names:
        return names

    suffixes = await _isis_config_suffixes(client, device)
    config_path = f"tailf-ncs:devices/device={device}/config"

    for suffix in suffixes:
        path = f"{config_path}/{suffix}"
        for depth in (3, 2):
            result = await call_mcp(
                client, "explore_nso_path", {"path": path, "depth": depth}
            )
            names = parse_configured_isis_interfaces(result)
            if names:
                return names

    for depth in (3, 2):
        result = await call_mcp(
            client, "explore_nso_path", {"path": config_path, "depth": depth}
        )
        names = parse_configured_isis_interfaces(result)
        if names:
            return names

    return []


async def _isis_config_suffixes(client: Any, device: str) -> list[str]:
    path = f"tailf-ncs:devices/device={device}/config"
    result = await call_mcp(client, "explore_nso_path", {"path": path, "depth": 1})
    data = unwrap_mcp_data(result)
    discovered: list[str] = []
    if isinstance(data, dict):
        for key in data:
            key_str = str(key)
            kl = key_str.lower()
            if "isis" in kl or key_str.endswith(":router"):
                discovered.append(key_str)
                if ":router" in key_str and "/isis" not in key_str:
                    discovered.append(f"{key_str}/isis")

    combined: list[str] = []
    for suffix in discovered + list(_ISIS_CONFIG_SUFFIXES):
        if suffix not in combined:
            combined.append(suffix)
    return combined


def parse_configured_isis_interfaces(explore_result: Any) -> list[str]:
    if isinstance(explore_result, dict) and explore_result.get("status") == "error":
        return []
    data = unwrap_mcp_data(explore_result)
    if isinstance(data, dict) and data.get("truncated"):
        return []

    names: list[str] = []
    _walk_for_isis_interfaces(data, names)
    return sorted(set(names))


def _walk_for_isis_interfaces(obj: Any, names: list[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_str = str(key).lower()
            if key_str in _ISIS_INTERFACE_KEYS or key_str.endswith(":interface"):
                _collect_isis_interface_list(value, names)
            elif "isis" in key_str and isinstance(value, (dict, list)):
                _walk_for_isis_interfaces(value, names)
            elif isinstance(value, (dict, list)):
                _walk_for_isis_interfaces(value, names)
    elif isinstance(obj, list):
        for item in obj:
            _walk_for_isis_interfaces(item, names)


def _collect_isis_interface_list(value: Any, names: list[str]) -> None:
    if isinstance(value, list):
        for item in value:
            name = _interface_name_from_entry(item)
            if name:
                names.append(name)
        return
    if isinstance(value, dict):
        for key in _ISIS_INTERFACE_KEYS:
            if key in value:
                _collect_isis_interface_list(value[key], names)
                return
        name = _interface_name_from_entry(value)
        if name:
            names.append(name)


def _interface_name_from_entry(entry: Any) -> str | None:
    if isinstance(entry, str) and entry.strip():
        return entry.strip()
    if isinstance(entry, dict):
        for key in ("interface-name", "name", "id"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


# Stands for the far interface of an adjacency whose far device was not read.
UNREAD_INTERFACE = "?"


def _unread_devices(issues: list[dict[str, Any]]) -> set[str]:
    """Devices whose IS-IS adjacencies could not be read, from this module's own issue messages."""
    return {
        str(issue.get("message") or "").split(":", 1)[0].strip()
        for issue in issues
        if issue.get("code") == "collection_error"
        and ("check_isis_adjacencies failed" in str(issue.get("message") or "")
             or isis_state_not_exposed(issue))
    }


def _one_sided_edge(
    obs: AdjacencyObservation,
    remote_device: str,
    physical_by_device: dict[str, set[str]] | None,
) -> dict[str, Any]:
    interface = canonical_interface_name(obs.local_device, obs.local_interface, physical_by_device)
    (dev_a, if_a), (dev_b, if_b) = ordered_isis_endpoints(
        obs.local_device, interface, remote_device, UNREAD_INTERFACE
    )
    seen = _normalize_isis_state(obs.state)
    near_is_a = dev_a == obs.local_device
    return {
        "id": isis_edge_id(dev_a, if_a, dev_b, if_b),
        "type": "isis_adjacency",
        "local": {"device": dev_a, "interface": if_a},
        "remote": {"device": dev_b, "interface": if_b},
        "state": {
            "local": seen if near_is_a else "unknown",
            "remote": "unknown" if near_is_a else seen,
            "status": "unknown",
            "detail": "far end not read this scan",
        },
    }


# The far end of an adjacency its device answered without.
NOT_LISTED = "not listed"


def _known_far_interface(known_edges: list[dict[str, Any]], device: str, interface: str, far_device: str) -> str:
    """The far interface of a known adjacency from this end, or ``UNREAD_INTERFACE`` when none is known."""
    for edge in known_edges:
        ends = [((edge.get(side) or {}).get("device"), (edge.get(side) or {}).get("interface"))
                for side in ("local", "remote")]
        for (near, near_if), (far, far_if) in (ends, ends[::-1]):
            if (near == device and far == far_device and near_if and far_if
                    and far_if != UNREAD_INTERFACE and interfaces_match(interface, near_if)):
                return far_if
    return UNREAD_INTERFACE


def _half_listed_edge(
    obs: AdjacencyObservation,
    remote_device: str,
    physical_by_device: dict[str, set[str]] | None,
    known_edges: list[dict[str, Any]],
) -> dict[str, Any]:
    interface = canonical_interface_name(obs.local_device, obs.local_interface, physical_by_device)
    far_interface = _known_far_interface(known_edges, obs.local_device, interface, remote_device)
    (dev_a, if_a), (dev_b, if_b) = ordered_isis_endpoints(
        obs.local_device, interface, remote_device, far_interface
    )
    seen = _normalize_isis_state(obs.state)
    states = {obs.local_device: seen, remote_device: NOT_LISTED}
    return {
        "id": isis_edge_id(dev_a, if_a, dev_b, if_b),
        "type": "isis_adjacency",
        "local": {"device": dev_a, "interface": if_a},
        "remote": {"device": dev_b, "interface": if_b},
        "state": {
            "local": states[dev_a],
            "remote": states[dev_b],
            "status": _bidirectional_status(seen, NOT_LISTED),
        },
    }


def _views_issue(edge: dict[str, Any]) -> dict[str, Any]:
    """Each router's view of an adjacency the two do not both have Up."""
    def view(end: dict[str, Any], state: str) -> str:
        device, interface = end.get("device"), end.get("interface")
        if state == NOT_LISTED:
            return (f"{device} does not list it (its IS-IS names no interface)" if interface == UNREAD_INTERFACE
                    else f"{device} {interface} does not list it")
        return f"{device} {interface} lists it as {state.capitalize()}"

    state = edge["state"]
    return {
        "severity": "high",
        "layer": "underlay",
        "code": "unidirectional_adjacency",
        "edge_id": edge["id"],
        "devices": [edge["local"]["device"], edge["remote"]["device"]],
        "message": (f"IS-IS adjacency down: {view(edge['local'], state['local'])}; "
                    f"{view(edge['remote'], state['remote'])}"),
    }


def _is_one_sided(edge: dict[str, Any]) -> bool:
    return UNREAD_INTERFACE in {
        (edge.get("local") or {}).get("interface"),
        (edge.get("remote") or {}).get("interface"),
    }


def _find_reverse_observation(
    rows: list[AdjacencyObservation],
    *,
    local_device: str,
    remote_device: str,
    used_remote: set[tuple[str, str]],
    device_names: list[str] | None = None,
) -> AdjacencyObservation | None:
    for obs in rows:
        if (local_device, obs.local_interface) in used_remote:
            continue
        neighbor = obs.neighbor_system_id
        # The same mapping as the forward direction, so a system id in another form still pairs.
        if (neighbor.lower() == remote_device.lower()
                or resolve_system_id(neighbor, device_names or [remote_device]) == remote_device):
            return obs
    return None


def _normalize_isis_state(state: str) -> str:
    return state.strip().lower()


def _bidirectional_status(state_a: str, state_b: str) -> str:
    up = "up"
    if state_a == up and state_b == up:
        return "up"
    if state_a != up and state_b != up:
        return "down"
    return "unidirectional"


def _edge_allowed_by_config(
    edge: dict[str, Any],
    isis_interfaces: dict[str, set[str]],
) -> bool:
    if not isis_interfaces:
        return True
    local = edge.get("local") or {}
    remote = edge.get("remote") or {}
    local_dev = local.get("device")
    local_if = local.get("interface")
    remote_dev = remote.get("device")
    remote_if = remote.get("interface")
    if not all([local_dev, local_if, remote_dev, remote_if]):
        return False
    local_set = isis_interfaces.get(str(local_dev), set())
    remote_set = isis_interfaces.get(str(remote_dev), set())
    if not local_set and not remote_set:
        return True
    # The unread end has no interface to check against its config.
    return all(
        interface == UNREAD_INTERFACE or _interface_in_isis_set(str(interface), configured)
        for interface, configured in ((local_if, local_set), (remote_if, remote_set))
    )


def _interface_in_isis_set(interface: str, configured: set[str]) -> bool:
    if interface in configured:
        return True
    return any(interfaces_match(interface, name) for name in configured)


# Why an expected adjacency missing now is Down, by where the expectation came from.
_EXPECTED_WHY = {
    "previous_scan": "up in the previous scan",
    "held": "still missing since an earlier scan",
}
MISSING_DETAIL = "not reported by either router; both answered"


def _missing_expected_edges(
    expected: list[dict[str, Any]],
    live: list[dict[str, Any]],
    *,
    device_names: list[str],
    unread: set[str],
    isis_interfaces: dict[str, set[str]],
) -> list[dict[str, Any]]:
    """Expected adjacencies absent from this pass that its answers and NSO config still call for.

    Both routers answered, neither interface is in another adjacency now (parallel
    links, recabling), and IS-IS is still configured on both interfaces.
    """
    live_ids = {edge["id"] for edge in live}
    live_ends = [((edge.get(side) or {}).get("device"), (edge.get(side) or {}).get("interface"))
                 for edge in live for side in ("local", "remote")]
    out: list[dict[str, Any]] = []
    for edge in expected:
        ends = [((edge.get(side) or {}).get("device"), (edge.get(side) or {}).get("interface"))
                for side in ("local", "remote")]
        if edge.get("id") in live_ids or not all(d and i and i != UNREAD_INTERFACE for d, i in ends):
            continue
        if any(d not in device_names or d in unread for d, _ in ends):
            continue
        if any(d == live_d and live_i not in {None, UNREAD_INTERFACE} and interfaces_match(i, live_i)
               for d, i in ends for live_d, live_i in live_ends):
            continue
        if not all(_interface_in_isis_set(i, isis_interfaces.get(d) or set()) for d, i in ends):
            continue
        out.append({"id": edge["id"], "type": "isis_adjacency", "local": edge["local"],
                    "remote": edge["remote"],
                    "meta": {"expected": "held" if edge.get("held") else "previous_scan"}})
    return out


def _static_adjacency_edge(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": edge["id"],
        "type": "isis_adjacency",
        "local": edge["local"],
        "remote": edge["remote"],
        "meta": edge.get("meta") or {},
    }


def _mcp_error(result: Any) -> str | None:
    if isinstance(result, dict) and result.get("status") == "error":
        return str(result.get("error_message") or "unknown error")
    data = unwrap_mcp_data(result)
    if isinstance(data, dict) and data.get("truncated"):
        return str(data.get("hint") or "response truncated")
    return None
