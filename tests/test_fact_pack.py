"""Tests for shared nso_facts.fact_pack builder."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nso_facts.fact_pack import build_fact_pack, collect_services_fact_slice


class _FakeClient:
    pass


@pytest.mark.asyncio
async def test_collect_services_fact_slice(monkeypatch):
    calls: list[tuple[str, dict | None]] = []

    async def fake_call(client, tool, params=None):
        calls.append((tool, params))
        if tool == "get_service_types":
            return {
                "status": "success",
                "data": {"service_types": ["l2ptp"]},
            }
        if tool == "get_fleet_sync_summary":
            return {
                "status": "success",
                "data": {
                    "summary": {"in_sync": 1, "out_of_sync": 0, "error": 0},
                    "devices": [{"device": "a", "result": "in-sync"}],
                },
            }
        if tool == "get_services":
            return {
                "status": "success",
                "data": {"services": [{"name": "svc1"}]},
            }
        if tool == "check_service_sync":
            return {"status": "success", "data": {"sync_state": "in-sync"}}
        raise AssertionError(f"unexpected tool {tool}")

    monkeypatch.setattr("nso_facts.fact_pack.call_mcp", fake_call)
    monkeypatch.setattr(
        "nso_facts.service_collect.call_mcp",
        fake_call,
    )
    # health helpers may call extract paths without MCP beyond check_service_sync
    settings = SimpleNamespace(
        ignore_service_types=frozenset(),
        max_service_types=10,
    )
    slice_ = await collect_services_fact_slice(_FakeClient(), settings)
    assert "l2ptp" in (slice_["services_by_type"] or {})
    assert "counts" in slice_
    assert slice_["fleet_sync"]["status"] == "success"
    assert any(t == "get_service_types" for t, _ in calls)


@pytest.mark.asyncio
async def test_build_fact_pack_composes_health_and_physical(monkeypatch):
    async def fake_services(client, settings):
        return {
            "service_types": {"status": "success"},
            "ignored_service_types": [],
            "fleet_sync": {"status": "success"},
            "services_by_type": {},
            "services": {},
            "counts": {},
        }

    async def fake_health(client, device_names, *, system=True, hardware=True):
        assert device_names == ["a", "b"]
        out = {"system_health": {}, "hardware_health": {}}
        if system:
            out["system_health"] = {"a": {"cpu": {"one_min": 1}}}
        if hardware:
            out["hardware_health"] = {"a": {"outcome": "ok"}}
        return out

    async def fake_phys(client, physical_edges, *, equivalences=None):
        assert physical_edges == [{"id": "p1"}]
        assert equivalences == {}
        return {
            "physical_operational_edges": [{"id": "p1", "state": {"status": "up"}}],
            "physical_issues": [],
        }

    monkeypatch.setattr(
        "nso_facts.fact_pack.collect_services_fact_slice", fake_services
    )
    monkeypatch.setattr(
        "nso_facts.fact_pack.collect_device_health_maps", fake_health
    )
    monkeypatch.setattr(
        "nso_facts.fact_pack.collect_physical_operational_slice", fake_phys
    )

    settings = SimpleNamespace(
        ignore_service_types=frozenset(),
        max_service_types=10,
    )
    pack = await build_fact_pack(
        _FakeClient(),
        settings,
        device_names=["a", "b"],
        include_capabilities=False,
        include_system_health=True,
        include_hardware_health=True,
        physical_edges=[{"id": "p1"}],
        include_physical_operational=True,
    )
    assert pack["device_names"] == ["a", "b"]
    assert pack["system_health"]["a"]["cpu"]["one_min"] == 1
    assert pack["hardware_health"]["a"]["outcome"] == "ok"
    assert pack["physical_operational_edges"][0]["id"] == "p1"


@pytest.mark.asyncio
async def test_build_fact_pack_requires_physical_edges_when_requested():
    settings = SimpleNamespace(
        ignore_service_types=frozenset(),
        max_service_types=10,
    )

    async def fake_services(client, settings):
        return {
            "service_types": {},
            "ignored_service_types": [],
            "fleet_sync": {},
            "services_by_type": {},
            "services": {},
            "counts": {},
        }

    import nso_facts.fact_pack as fp

    # Avoid real MCP for the services slice
    original = fp.collect_services_fact_slice
    fp.collect_services_fact_slice = fake_services  # type: ignore[assignment]
    try:
        with pytest.raises(ValueError, match="physical_edges"):
            await build_fact_pack(
                _FakeClient(),
                settings,
                device_names=[],
                include_system_health=False,
                include_hardware_health=False,
                include_physical_operational=True,
            )
    finally:
        fp.collect_services_fact_slice = original  # type: ignore[assignment]
