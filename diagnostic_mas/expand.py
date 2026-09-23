"""Expand a device focus set to one-hop IS-IS / BGP peers (not the full pocket)."""

from __future__ import annotations

from typing import Any

from nso_facts.topology import routing as bgp_mod
from nso_facts.topology import underlay as isis_mod


def _ordered_union(seeds: list[str], peers: set[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for name in list(seeds) + sorted(peers):
        if name and name not in seen:
            out.append(name)
            seen.add(name)
    return out


async def _bgp_neighbor_ips(client: Any, seeds: list[str]) -> set[str]:
    """Live + config neighbor addresses seen on seed devices."""
    ips: set[str] = set()
    observations, _, _ = await bgp_mod._collect_observations(client, seeds)
    for obs in observations:
        if obs.neighbor_address:
            ips.add(str(obs.neighbor_address).strip())
    configs = await bgp_mod._neighbor_configs_for_devices(client, seeds)
    for neighbors in configs.values():
        for cfg in neighbors:
            addr = getattr(cfg, "neighbor_address", None)
            if addr:
                ips.add(str(addr).strip())
    return {ip for ip in ips if ip}


async def _map_bgp_ips_to_devices(
    client: Any,
    neighbor_ips: set[str],
    candidates: list[str],
) -> set[str]:
    """Resolve neighbor IPs → devices by probing router-ids until all mapped.

    Stops early once every neighbor IP is accounted for — does not require
    probing the rest of the inventory.
    """
    if not neighbor_ips or not candidates:
        return set()
    remaining = set(neighbor_ips)
    peers: set[str] = set()
    for device in candidates:
        if not remaining:
            break
        enriched = await bgp_mod._enrich_router_ids_from_live(client, [device], {})
        rid = enriched.get(device)
        if not rid:
            static_ids = await bgp_mod._router_ids_for_devices(client, [device])
            rid = static_ids.get(device)
        if rid and rid in remaining:
            peers.add(device)
            remaining.discard(rid)
    return peers


async def _isis_peer_devices(
    client: Any,
    seeds: list[str],
    inventory: list[str],
) -> set[str]:
    """Map IS-IS neighbor system-ids to inventory names (string match only)."""
    observations, _, _ = await isis_mod._collect_observations(client, seeds)
    peers: set[str] = set()
    seed_set = set(seeds)
    for obs in observations:
        remote = isis_mod.resolve_system_id(obs.neighbor_system_id, inventory)
        if remote and remote not in seed_set:
            peers.add(remote)
    return peers


async def expand_spine_devices(
    client: Any,
    seeds: list[str],
    inventory: list[str],
    *,
    expand_isis: bool = False,
    expand_bgp: bool = False,
) -> list[str]:
    """Return seed devices plus one-hop peers needed for bidirectional checks.

    - IS-IS: resolve neighbor system-ids against the inventory name list (no
      extra device queries beyond the seed adjacency check).
    - BGP: read neighbor IPs on seeds, then probe other inventory devices for
      matching router-ids **until those IPs are mapped** (early exit).
    """
    if not seeds:
        return list(inventory)
    if not expand_isis and not expand_bgp:
        return list(seeds)

    inv = list(inventory)
    seed_set = set(seeds)
    peers: set[str] = set()

    if expand_isis:
        peers |= await _isis_peer_devices(client, seeds, inv)

    if expand_bgp:
        ips = await _bgp_neighbor_ips(client, seeds)
        # Drop IPs that already belong to seeds
        seed_rids = await bgp_mod._enrich_router_ids_from_live(client, seeds, {})
        seed_rids.update(await bgp_mod._router_ids_for_devices(client, seeds))
        for rid in seed_rids.values():
            ips.discard(rid)
        candidates = [d for d in inv if d not in seed_set]
        peers |= await _map_bgp_ips_to_devices(client, ips, candidates)

    return _ordered_union(seeds, peers)
