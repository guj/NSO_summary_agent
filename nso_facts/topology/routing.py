"""Layer 2 — routing (BGP sessions from config + live state)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from nso_facts.mcp_client import call_mcp, unwrap_mcp_data
from nso_facts.topology.graph import (
    bgp_edge_id,
    ordered_bgp_endpoints,
    summarize_operational_routing,
    summarize_static_routing,
)

_BGP_CONFIG_SUFFIXES = (
    "tailf-ned-cisco-ios-xr:router/bgp",
    "tailf-ned-cisco-ios-xr:router",
    "tailf-ned-cisco-ios:router/bgp",
)

_BGP_SUMMARY_COMMANDS = (
    "bgp ipv4 unicast summary",
    "bgp summary",
)

_BGP_NEIGHBOR_LINE = re.compile(
    r"^\s*(?P<neighbor>\d+\.\d+\.\d+\.\d+)\s+(?P<rest>.+)$"
)

_BGP_STATE_TOKEN = re.compile(
    r"\b(Established|Estab|Idle|Active|Connect|OpenSent|OpenConfirm)\b",
    re.IGNORECASE,
)

_BGP_ROUTER_ID = re.compile(
    r"BGP router identifier\s+(?P<router_id>\d+\.\d+\.\d+\.\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BgpNeighborConfig:
    device: str
    neighbor_address: str
    remote_as: int | None
    local_as: int | None
    vrf: str | None = None


@dataclass(frozen=True)
class BgpSessionObservation:
    device: str
    neighbor_address: str
    state: str


async def collect_static_routing(
    client: Any,
    device_names: list[str],
    *,
    seed_from_live: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Build static BGP session edges for topology.static.json.

    BGP intent is read from ``get_device_config`` (one call per device).
    ``explore_nso_path`` is fallback only — shallow depth often returns
    empty neighbor lists; see DESIGN.md § routing / BGP.
    """
    issues: list[dict[str, Any]] = []
    neighbor_configs: dict[str, list[BgpNeighborConfig]] = {}
    router_ids: dict[str, str] = {}
    devices_queried = 0
    devices_failed = 0

    for device in device_names:
        try:
            neighbors, router_id = await _static_bgp_for_device(client, device)
            neighbor_configs[device] = neighbors
            if router_id:
                router_ids[device] = router_id
            devices_queried += 1
            if not neighbors:
                issues.append(
                    {
                        "severity": "low",
                        "layer": "routing",
                        "code": "no_configured_bgp",
                        "edge_id": None,
                        "message": f"{device}: no BGP neighbors found in NSO config",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            devices_failed += 1
            neighbor_configs[device] = []
            issues.append(
                {
                    "severity": "medium",
                    "layer": "routing",
                    "code": "collection_error",
                    "edge_id": None,
                    "message": f"{device}: static BGP discovery failed: {exc}",
                }
            )

    edges: list[dict[str, Any]] = []
    config_paired, config_pair_issues = pair_bgp_configs(
        neighbor_configs, router_ids
    )
    issues.extend(config_pair_issues)
    edges_by_id = {
        edge["id"]: _static_session_edge(edge) for edge in config_paired
    }

    if seed_from_live and device_names:
        observations, live_issues, _ = await _collect_observations(client, device_names)
        issues.extend(live_issues)
        router_ids = await _enrich_router_ids_from_live(
            client, device_names, router_ids
        )
        paired, pair_issues = pair_bgp_observations(
            observations,
            neighbor_configs,
            router_ids,
        )
        issues.extend(pair_issues)
        for edge in paired:
            if _edge_allowed_by_config(edge, neighbor_configs):
                edges_by_id[edge["id"]] = _static_session_edge(edge)
            else:
                issues.append(
                    {
                        "severity": "low",
                        "layer": "routing",
                        "code": "live_session_not_in_config",
                        "edge_id": edge["id"],
                        "message": (
                            f"Live BGP session {edge['id']} not fully reflected "
                            "in BGP neighbor config"
                        ),
                    }
                )
    edges = list(edges_by_id.values())

    coverage = {
        "devices_total": len(device_names),
        "devices_queried": devices_queried,
        "devices_failed": devices_failed,
    }
    return edges, issues, coverage


async def collect_operational_routing(
    client: Any,
    device_names: list[str],
    static_edges: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Refresh live BGP session state and compare to static edge set."""
    observations, issues, coverage = await _collect_observations(client, device_names)
    neighbor_configs = await _neighbor_configs_for_devices(client, device_names)
    router_ids = await _router_ids_for_devices(client, device_names)
    router_ids = await _enrich_router_ids_from_live(client, device_names, router_ids)
    paired, pair_issues = pair_bgp_observations(
        observations,
        neighbor_configs,
        router_ids,
    )
    issues.extend(pair_issues)

    static_by_id = {edge["id"]: edge for edge in static_edges}
    operational: list[dict[str, Any]] = []

    for edge in paired:
        operational.append({"id": edge["id"], "state": edge["state"]})

    paired_ids = {edge["id"] for edge in paired}
    for static_id, static_edge in static_by_id.items():
        if static_id in paired_ids:
            continue
        operational.append(
            {
                "id": static_id,
                "state": {
                    "local": "unknown",
                    "remote": "unknown",
                    "status": "down",
                    "detail": "no operational BGP session observed",
                },
            }
        )
        local = static_edge.get("local") or {}
        remote = static_edge.get("remote") or {}
        issues.append(
            {
                "severity": "high",
                "layer": "routing",
                "code": "configured_no_session",
                "edge_id": static_id,
                "message": (
                    f"Configured/static BGP session missing live Established state: "
                    f"{local.get('device')} {local.get('address')} ↔ "
                    f"{remote.get('device')} {remote.get('address')}"
                ),
            }
        )

    for edge in paired:
        if edge["id"] not in static_by_id:
            issues.append(
                {
                    "severity": "low",
                    "layer": "routing",
                    "code": "unexpected_live_object",
                    "edge_id": edge["id"],
                    "message": f"Live BGP session not in static topology: {edge['id']}",
                }
            )

    return operational, issues, coverage


def build_static_routing_layer(edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": edges, "summary": summarize_static_routing(edges)}


def build_operational_routing_layer(edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": edges, "summary": summarize_operational_routing(edges)}


def pair_bgp_observations(
    observations: list[BgpSessionObservation],
    neighbor_configs: dict[str, list[BgpNeighborConfig]],
    router_ids: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair one-way BGP summary rows into bidirectional routing edges."""
    issues: list[dict[str, Any]] = []
    by_device: dict[str, list[BgpSessionObservation]] = {}
    for obs in observations:
        by_device.setdefault(obs.device, []).append(obs)

    used_remote: set[tuple[str, str]] = set()
    edges: list[dict[str, Any]] = []

    for obs_a in observations:
        remote_device = resolve_neighbor_device(
            obs_a.neighbor_address,
            neighbor_configs,
            router_ids,
        )
        if remote_device is None:
            issues.append(
                {
                    "severity": "medium",
                    "layer": "routing",
                    "code": "unknown_neighbor_address",
                    "edge_id": None,
                    "message": (
                        f"{obs_a.device} {obs_a.neighbor_address}: "
                        "could not map neighbor address to NSO device"
                    ),
                }
            )
            continue
        if remote_device == obs_a.device:
            continue

        obs_b = _find_reverse_observation(
            by_device.get(remote_device, []),
            local_device=remote_device,
            remote_device=obs_a.device,
            neighbor_configs=neighbor_configs,
            router_ids=router_ids,
            used_remote=used_remote,
        )
        if obs_b is None:
            issues.append(
                {
                    "severity": "medium",
                    "layer": "routing",
                    "code": "missing_reverse_session",
                    "edge_id": None,
                    "message": (
                        f"{obs_a.device} sees {obs_a.neighbor_address} "
                        f"({obs_a.state}) but {remote_device} has no matching "
                        "reciprocal row in bgp summary"
                    ),
                }
            )
            continue

        local_address = router_ids.get(obs_a.device, obs_a.neighbor_address)
        remote_address = router_ids.get(remote_device, obs_b.neighbor_address)
        (dev_a, addr_a), (dev_b, addr_b) = ordered_bgp_endpoints(
            obs_a.device,
            local_address,
            remote_device,
            remote_address,
        )
        edge_id = bgp_edge_id(dev_a, addr_a, dev_b, addr_b)
        if any(existing["id"] == edge_id for existing in edges):
            continue

        state_a = _normalize_bgp_state(obs_a.state)
        state_b = _normalize_bgp_state(obs_b.state)
        status = _bidirectional_status(state_a, state_b)
        meta = _session_meta(neighbor_configs, obs_a.device, obs_a.neighbor_address)
        edges.append(
            {
                "id": edge_id,
                "type": "bgp_session",
                "local": {"device": dev_a, "address": addr_a},
                "remote": {"device": dev_b, "address": addr_b},
                "meta": meta,
                "state": {
                    "local": state_a,
                    "remote": state_b,
                    "status": status,
                },
            }
        )
        used_remote.add((remote_device, obs_b.neighbor_address))

        if status == "degraded":
            issues.append(
                {
                    "severity": "high",
                    "layer": "routing",
                    "code": "one_sided_session",
                    "edge_id": edge_id,
                    "message": (
                        f"BGP session {dev_a} {addr_a} ({state_a}) ↔ "
                        f"{dev_b} {addr_b} ({state_b})"
                    ),
                }
            )

    return edges, issues


def pair_bgp_configs(
    neighbor_configs: dict[str, list[BgpNeighborConfig]],
    router_ids: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build static BGP edges from config only (no live state)."""
    issues: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()

    for device, neighbors in neighbor_configs.items():
        local_address = router_ids.get(device, "")
        for cfg in neighbors:
            remote_device = resolve_neighbor_device(
                cfg.neighbor_address,
                neighbor_configs,
                router_ids,
            )
            if remote_device is None or remote_device == device:
                continue
            remote_address = router_ids.get(remote_device, cfg.neighbor_address)
            if not local_address:
                local_address = cfg.neighbor_address
            (dev_a, addr_a), (dev_b, addr_b) = ordered_bgp_endpoints(
                device,
                local_address,
                remote_device,
                remote_address,
            )
            edge_id = bgp_edge_id(dev_a, addr_a, dev_b, addr_b)
            if edge_id in seen:
                continue
            seen.add(edge_id)
            edges.append(
                {
                    "id": edge_id,
                    "type": "bgp_session",
                    "local": {"device": dev_a, "address": addr_a},
                    "remote": {"device": dev_b, "address": addr_b},
                    "meta": _session_meta(neighbor_configs, device, cfg.neighbor_address),
                }
            )

    if not edges and any(neighbor_configs.values()):
        issues.append(
            {
                "severity": "low",
                "layer": "routing",
                "code": "unpaired_bgp_config",
                "edge_id": None,
                "message": (
                    "BGP neighbors found in config but could not pair into sessions "
                    "(missing router-id mapping)"
                ),
            }
        )
    return edges, issues


def resolve_neighbor_device(
    neighbor_address: str,
    neighbor_configs: dict[str, list[BgpNeighborConfig]],
    router_ids: dict[str, str],
) -> str | None:
    """Map a BGP neighbor address to the NSO device that owns that address."""
    for device, router_id in router_ids.items():
        if router_id == neighbor_address:
            return device
    return None


async def probe_static_bgp_discovery(
    client: Any,
    device: str,
) -> dict[str, Any]:
    """Debug BGP config discovery for one device."""
    attempts: list[dict[str, Any]] = []

    result = await call_mcp(client, "get_device_config", {"device_name": device})
    neighbors, router_id = parse_configured_bgp(result, device=device)
    attempts.append(
        {
            "tool": "get_device_config",
            "neighbor_count": len(neighbors),
            "router_id": router_id,
            "sample_neighbors": [n.neighbor_address for n in neighbors[:5]],
            "error": _mcp_error(result),
        }
    )
    if neighbors or router_id:
        return {
            "device": device,
            "selected": attempts[-1],
            "attempts": attempts,
        }

    suffixes = await _bgp_config_suffixes(client, device)
    config_path = f"tailf-ncs:devices/device={device}/config"
    for suffix in suffixes:
        path = f"{config_path}/{suffix}"
        for depth in (3, 2):
            result = await call_mcp(
                client, "explore_nso_path", {"path": path, "depth": depth}
            )
            neighbors, router_id = parse_configured_bgp(result, device=device)
            attempts.append(
                {
                    "tool": "explore_nso_path",
                    "path": path,
                    "depth": depth,
                    "neighbor_count": len(neighbors),
                    "router_id": router_id,
                    "sample_neighbors": [n.neighbor_address for n in neighbors[:5]],
                    "error": _mcp_error(result),
                }
            )
            if neighbors or router_id:
                return {
                    "device": device,
                    "discovered_suffixes": suffixes,
                    "selected": attempts[-1],
                    "attempts": attempts,
                }

    return {
        "device": device,
        "discovered_suffixes": suffixes,
        "selected": attempts[-1] if attempts else None,
        "attempts": attempts,
    }


def parse_router_id_from_summary(text: str) -> str | None:
    match = _BGP_ROUTER_ID.search(text)
    if match:
        return match.group("router_id")
    return None


async def _enrich_router_ids_from_live(
    client: Any,
    device_names: list[str],
    router_ids: dict[str, str],
) -> dict[str, str]:
    enriched = dict(router_ids)
    for device in device_names:
        if device in enriched:
            continue
        for command in _BGP_SUMMARY_COMMANDS:
            result = await call_mcp(
                client,
                "exec_show",
                {"device_name": device, "input_command": command},
            )
            if isinstance(result, dict) and result.get("status") == "error":
                continue
            text = _exec_show_text(result)
            router_id = parse_router_id_from_summary(text)
            if router_id:
                enriched[device] = router_id
                break
    return enriched


async def _collect_observations(
    client: Any,
    device_names: list[str],
) -> tuple[list[BgpSessionObservation], list[dict[str, Any]], dict[str, int]]:
    observations: list[BgpSessionObservation] = []
    issues: list[dict[str, Any]] = []
    devices_queried = 0
    devices_failed = 0

    for device in device_names:
        try:
            observations.extend(await _observations_for_device(client, device))
            devices_queried += 1
        except Exception as exc:  # noqa: BLE001
            devices_failed += 1
            issues.append(
                {
                    "severity": "medium",
                    "layer": "routing",
                    "code": "collection_error",
                    "edge_id": None,
                    "message": f"{device}: BGP summary collection failed: {exc}",
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
) -> list[BgpSessionObservation]:
    last_error: str | None = None
    observations: list[BgpSessionObservation] = []
    seen: set[tuple[str, str]] = set()

    for command in _BGP_SUMMARY_COMMANDS:
        result = await call_mcp(
            client,
            "exec_show",
            {"device_name": device, "input_command": command},
        )
        if isinstance(result, dict) and result.get("status") == "error":
            last_error = str(result.get("error_message") or "exec_show failed")
            continue
        text = _exec_show_text(result)
        for obs in parse_bgp_summary_text(device, text):
            key = (obs.neighbor_address, obs.state)
            if key not in seen:
                seen.add(key)
                observations.append(obs)
        if observations:
            return observations
    if last_error:
        raise RuntimeError(last_error)
    return observations


async def _static_bgp_for_device(
    client: Any,
    device: str,
) -> tuple[list[BgpNeighborConfig], str | None]:
    """Load BGP intent from full device config; explore_nso_path is fallback only."""
    result = await call_mcp(client, "get_device_config", {"device_name": device})
    if not _mcp_error(result):
        neighbors, router_id = parse_configured_bgp(result, device=device)
        if neighbors or router_id:
            return neighbors, router_id

    suffixes = await _bgp_config_suffixes(client, device)
    config_path = f"tailf-ncs:devices/device={device}/config"
    router_id: str | None = None

    for suffix in suffixes:
        path = f"{config_path}/{suffix}"
        for depth in (3, 2):
            result = await call_mcp(
                client, "explore_nso_path", {"path": path, "depth": depth}
            )
            if _mcp_error(result):
                continue
            neighbors, parsed_router_id = parse_configured_bgp(result, device=device)
            if parsed_router_id:
                router_id = parsed_router_id
            if neighbors:
                return neighbors, router_id

    return [], router_id


async def _bgp_config_suffixes(client: Any, device: str) -> list[str]:
    path = f"tailf-ncs:devices/device={device}/config"
    result = await call_mcp(client, "explore_nso_path", {"path": path, "depth": 1})
    data = unwrap_mcp_data(result)
    discovered: list[str] = []
    if isinstance(data, dict):
        for key in data:
            key_str = str(key)
            kl = key_str.lower()
            if "bgp" in kl or key_str.endswith(":router"):
                discovered.append(key_str)
                if ":router" in key_str and "/bgp" not in key_str:
                    discovered.append(f"{key_str}/bgp")

    combined: list[str] = []
    for suffix in discovered + list(_BGP_CONFIG_SUFFIXES):
        if suffix not in combined:
            combined.append(suffix)
    combined.sort(key=lambda suffix: (0 if suffix.endswith("/bgp") else 1, suffix))
    return combined


def parse_configured_bgp(
    explore_result: Any,
    *,
    device: str = "",
) -> tuple[list[BgpNeighborConfig], str | None]:
    if isinstance(explore_result, dict) and explore_result.get("status") == "error":
        return [], None
    data = unwrap_mcp_data(explore_result)
    if isinstance(data, dict) and data.get("truncated"):
        return [], None

    neighbors: list[BgpNeighborConfig] = []
    router_id: str | None = None
    _walk_for_bgp_config(data, device, neighbors)
    router_id = _extract_router_id(data) or router_id
    return neighbors, router_id


def parse_bgp_summary_text(
    device: str,
    text: str,
) -> list[BgpSessionObservation]:
    observations: list[BgpSessionObservation] = []
    for line in text.splitlines():
        match = _BGP_NEIGHBOR_LINE.match(line)
        if not match:
            continue
        neighbor = match.group("neighbor")
        state = _state_from_bgp_summary_rest(match.group("rest"))
        observations.append(BgpSessionObservation(device, neighbor, state))
    return observations


def _state_from_bgp_summary_rest(rest: str) -> str:
    """Derive BGP peer state from the columns after the neighbor address.

    IOS-XR ``show bgp summary`` St/PfxRcd column is either a FSM word
    (Idle/Active/…) or a prefix count when the session is Established.
    Some releases also print ``200160 (Estab)``.
    """
    token = _BGP_STATE_TOKEN.search(rest)
    if token:
        return token.group(1)
    lowered = rest.lower()
    if "(estab)" in lowered.replace(" ", ""):
        return "Established"
    if "never" in lowered:
        return "Idle"
    # Established peers: last field is prefixes received (e.g. "0", "445667").
    last = rest.split()[-1] if rest.split() else ""
    if last.isdigit():
        return "Established"
    return "unknown"


def _exec_show_text(result: Any) -> str:
    """Normalize exec_show MCP payloads to CLI text (handles dict or bare string)."""
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    if result.get("status") == "error":
        return ""
    payload: Any = result
    if result.get("status") == "success":
        payload = result.get("data")
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    for key in ("result", "output", "stdout"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for value in payload.values():
        if isinstance(value, dict):
            nested = value.get("result")
            if isinstance(nested, str) and nested.strip():
                return nested
    return ""


def _walk_for_bgp_config(
    obj: Any,
    device: str,
    neighbors: list[BgpNeighborConfig],
) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_str = str(key).lower()
            if key_str.endswith(":bgp") or key_str == "bgp":
                _collect_bgp_neighbors(value, device, neighbors)
            elif "bgp" in key_str and isinstance(value, (dict, list)):
                _walk_for_bgp_config(value, device, neighbors)
            elif isinstance(value, (dict, list)):
                _walk_for_bgp_config(value, device, neighbors)
    elif isinstance(obj, list):
        for item in obj:
            _walk_for_bgp_config(item, device, neighbors)


def _collect_bgp_neighbors(
    value: Any,
    device: str,
    neighbors: list[BgpNeighborConfig],
) -> None:
    if not isinstance(value, dict):
        return
    for inst_key in ("bgp-no-instance", "instance", "instances"):
        instances = value.get(inst_key)
        if instances is None:
            continue
        if isinstance(instances, dict):
            instances = [instances]
        if not isinstance(instances, list):
            continue
        for inst in instances:
            if not isinstance(inst, dict):
                continue
            local_as = _as_int(inst.get("id") or inst.get("as"))
            vrf = inst.get("vrf")
            vrf_name = vrf.get("id") if isinstance(vrf, dict) else None
            neighbor_rows = inst.get("neighbor") or inst.get("neighbors")
            if isinstance(neighbor_rows, dict):
                neighbor_rows = [neighbor_rows]
            if not isinstance(neighbor_rows, list):
                continue
            for row in neighbor_rows:
                if not isinstance(row, dict):
                    continue
                address = row.get("id") or row.get("neighbor-address")
                if not isinstance(address, str) or not address.strip():
                    continue
                remote_as = _as_int(row.get("remote-as") or row.get("remote_as"))
                neighbors.append(
                    BgpNeighborConfig(
                        device=device,
                        neighbor_address=address.strip(),
                        remote_as=remote_as,
                        local_as=local_as,
                        vrf=vrf_name if isinstance(vrf_name, str) else None,
                    )
                )


def _extract_router_id(data: Any) -> str | None:
    if isinstance(data, dict):
        for key in ("router-id", "router_id", "bgp-router-id"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in data.values():
            found = _extract_router_id(value)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _extract_router_id(item)
            if found:
                return found
    return None


def _find_reverse_observation(
    rows: list[BgpSessionObservation],
    *,
    local_device: str,
    remote_device: str,
    neighbor_configs: dict[str, list[BgpNeighborConfig]],
    router_ids: dict[str, str],
    used_remote: set[tuple[str, str]],
) -> BgpSessionObservation | None:
    target = router_ids.get(remote_device)
    for obs in rows:
        if (local_device, obs.neighbor_address) in used_remote:
            continue
        if target and obs.neighbor_address == target:
            return obs
        if any(
            cfg.neighbor_address == obs.neighbor_address
            for cfg in neighbor_configs.get(local_device, [])
        ):
            mapped = resolve_neighbor_device(
                obs.neighbor_address,
                neighbor_configs,
                router_ids,
            )
            if mapped == remote_device:
                return obs
    return None


def _normalize_bgp_state(state: str) -> str:
    normalized = state.strip().lower()
    if normalized in {"established", "estab"}:
        return "established"
    return normalized


def _bidirectional_status(state_a: str, state_b: str) -> str:
    up = "established"
    if state_a == up and state_b == up:
        return "up"
    if state_a != up and state_b != up:
        return "down"
    return "degraded"


def _edge_allowed_by_config(
    edge: dict[str, Any],
    neighbor_configs: dict[str, list[BgpNeighborConfig]],
) -> bool:
    if not neighbor_configs:
        return True
    local = edge.get("local") or {}
    remote = edge.get("remote") or {}
    local_dev = local.get("device")
    remote_dev = remote.get("device")
    local_addr = local.get("address")
    remote_addr = remote.get("address")
    if not all([local_dev, remote_dev]):
        return False
    return _device_peers_with(
        str(local_dev), str(remote_addr), neighbor_configs
    ) and _device_peers_with(str(remote_dev), str(local_addr), neighbor_configs)


def _device_peers_with(
    device: str,
    neighbor_address: str | None,
    neighbor_configs: dict[str, list[BgpNeighborConfig]],
) -> bool:
    if not neighbor_address:
        return False
    return any(
        cfg.neighbor_address == neighbor_address
        for cfg in neighbor_configs.get(device, [])
    )


def _session_meta(
    neighbor_configs: dict[str, list[BgpNeighborConfig]],
    device: str,
    neighbor_address: str,
) -> dict[str, Any]:
    for cfg in neighbor_configs.get(device, []):
        if cfg.neighbor_address == neighbor_address:
            meta: dict[str, Any] = {}
            if cfg.remote_as is not None:
                meta["remote_as"] = cfg.remote_as
            if cfg.local_as is not None:
                meta["local_as"] = cfg.local_as
            if cfg.vrf:
                meta["vrf"] = cfg.vrf
            return meta
    return {}


def _static_session_edge(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": edge["id"],
        "type": "bgp_session",
        "local": edge["local"],
        "remote": edge["remote"],
        "meta": edge.get("meta") or {},
    }


async def _neighbor_configs_for_devices(
    client: Any,
    device_names: list[str],
) -> dict[str, list[BgpNeighborConfig]]:
    configs: dict[str, list[BgpNeighborConfig]] = {}
    for device in device_names:
        try:
            neighbors, _ = await _static_bgp_for_device(client, device)
            configs[device] = neighbors
        except Exception:  # noqa: BLE001
            configs[device] = []
    return configs


async def _router_ids_for_devices(
    client: Any,
    device_names: list[str],
) -> dict[str, str]:
    router_ids: dict[str, str] = {}
    for device in device_names:
        try:
            _, router_id = await _static_bgp_for_device(client, device)
            if router_id:
                router_ids[device] = router_id
        except Exception:  # noqa: BLE001
            continue
    return router_ids


def _as_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _mcp_error(result: Any) -> str | None:
    if isinstance(result, dict) and result.get("status") == "error":
        return str(result.get("error_message") or "unknown error")
    data = unwrap_mcp_data(result)
    if isinstance(data, dict) and data.get("truncated"):
        return str(data.get("hint") or "response truncated")
    return None
