"""Tests for seed → peer expansion (not full-inventory collection)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from diagnostic_mas.expand import expand_spine_devices
from nso_facts.topology.routing import BgpNeighborConfig, BgpSessionObservation
from nso_facts.topology.underlay import AdjacencyObservation


@pytest.mark.asyncio
async def test_expand_bgp_adds_only_mapped_peers():
    seeds = ["renc-data-sw"]
    inventory = ["renc-data-sw", "lbnl-data-sw", "uky-data-sw", "other-sw"]

    seed_obs = [
        BgpSessionObservation(
            device="renc-data-sw",
            neighbor_address="10.0.0.2",
            state="Established",
        ),
        BgpSessionObservation(
            device="renc-data-sw",
            neighbor_address="10.0.0.3",
            state="Established",
        ),
    ]

    async def fake_bgp_obs(client, devices):
        if devices == seeds:
            return seed_obs, [], {"devices_queried": 1}
        return [], [], {"devices_queried": 0}

    async def fake_configs(client, devices):
        return {d: [] for d in devices}

    async def fake_enrich(client, devices, router_ids):
        out = dict(router_ids)
        for d in devices:
            if d == "renc-data-sw":
                out[d] = "10.0.0.1"
            elif d == "lbnl-data-sw":
                out[d] = "10.0.0.2"
            elif d == "uky-data-sw":
                out[d] = "10.0.0.3"
            # other-sw never needed if early-exit works after two peers
        return out

    async def fake_static_rids(client, devices):
        return {}

    with (
        patch(
            "diagnostic_mas.expand.bgp_mod._collect_observations",
            new=AsyncMock(side_effect=fake_bgp_obs),
        ),
        patch(
            "diagnostic_mas.expand.bgp_mod._neighbor_configs_for_devices",
            new=AsyncMock(side_effect=fake_configs),
        ),
        patch(
            "diagnostic_mas.expand.bgp_mod._enrich_router_ids_from_live",
            new=AsyncMock(side_effect=fake_enrich),
        ),
        patch(
            "diagnostic_mas.expand.bgp_mod._router_ids_for_devices",
            new=AsyncMock(side_effect=fake_static_rids),
        ),
    ):
        out = await expand_spine_devices(
            object(),
            seeds,
            inventory,
            expand_bgp=True,
            expand_isis=False,
        )

    assert out[0] == "renc-data-sw"
    assert set(out) == {"renc-data-sw", "lbnl-data-sw", "uky-data-sw"}
    assert "other-sw" not in out


@pytest.mark.asyncio
async def test_expand_isis_uses_inventory_name_match():
    seeds = ["renc-data-sw"]
    inventory = ["renc-data-sw", "lbnl-data-sw", "uky-data-sw"]
    obs = [
        AdjacencyObservation(
            local_device="renc-data-sw",
            local_interface="Hu0/0/0/0",
            neighbor_system_id="lbnl-data-sw",
            state="Up",
        ),
    ]

    async def fake_isis_obs(client, devices):
        return obs, [], {}

    with patch(
        "diagnostic_mas.expand.isis_mod._collect_observations",
        new=AsyncMock(side_effect=fake_isis_obs),
    ):
        out = await expand_spine_devices(
            object(),
            seeds,
            inventory,
            expand_isis=True,
            expand_bgp=False,
        )

    assert out == ["renc-data-sw", "lbnl-data-sw"]
