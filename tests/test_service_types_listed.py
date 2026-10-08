"""Which service types a scan listed in full, including types with no instances."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nso_facts.health import listed_service_types


def test_a_type_is_listed_only_when_its_listing_came_back_whole():
    listings = {
        "l2bridge": {"status": "success", "data": {"services": [{"name": "a"}]}},
        "port-mirror": {"status": "success", "data": {"services": []}},
        "l3rt": {"status": "error", "data": {"services": []}},
        "l2sts": {"error": "timed out"},
        "l2ptp": {"status": "success", "data": {}},
        "l3vpn": {"status": "success", "data": {"services": [], "has_more": True}},
    }

    assert listed_service_types(listings) == ["l2bridge", "port-mirror"]


@pytest.mark.asyncio
async def test_service_spine_records_the_types_it_listed(monkeypatch):
    from diagnostic_mas.roles import service as role

    async def fake_spine(client, settings, device_names, **kwargs):
        return {
            "services": {"l2bridge/a": {"name": "a", "service_type": "l2bridge", "devices": ["pe1"]}},
            "services_by_type": {
                "l2bridge": {"status": "success", "data": {"services": [{"name": "a"}]}},
                "port-mirror": {"status": "success", "data": {"services": []}},
                "l3rt": {"error": "timed out"},
            },
        }

    monkeypatch.setattr(role, "collect_fleet_spine", fake_spine)

    result = await role.run_service_spine(None, SimpleNamespace(), ["pe1"], [])

    assert result["extra"]["service_types_listed"] == ["l2bridge", "port-mirror"]
