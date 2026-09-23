"""Service-sync mode: check (default) vs skip (endpoint fleet sync fallback)."""

from __future__ import annotations

import pytest

from nso_facts.service_collect import (
    SERVICE_SYNC_SKIP_NOTE,
    collect_service_health,
    normalize_service_sync_mode,
)


class _FakeClient:
    pass


def test_normalize_service_sync_mode():
    assert normalize_service_sync_mode(None) == "check"
    assert normalize_service_sync_mode("check") == "check"
    assert normalize_service_sync_mode("SKIP") == "skip"
    assert normalize_service_sync_mode("bogus") == "check"


@pytest.mark.asyncio
async def test_skip_mode_does_not_call_check_service_sync(monkeypatch):
    calls: list[str] = []

    async def fake_mcp(client, tool, args=None):
        calls.append(tool)
        raise AssertionError(f"unexpected MCP tool {tool}")

    monkeypatch.setattr("nso_facts.service_collect.call_mcp", fake_mcp)

    services_by_type = {
        "l3rt": {
            "status": "success",
            "data": {
                "services": [
                    {"name": "svc-a", "device": "gpn-data-sw"},
                    {"name": "svc-b", "device": "atla-data-sw"},
                ]
            },
        }
    }
    out = await collect_service_health(
        _FakeClient(),
        services_by_type,
        {"l3rt": "l3rt"},
        {"gpn-data-sw": "in-sync", "atla-data-sw": "in-sync"},
        service_sync_mode="skip",
    )
    assert calls == []
    assert out["l3rt/svc-a"]["status"] == "up"
    assert out["l3rt/svc-a"]["system_status"] == "up"
    assert out["l3rt/svc-a"]["in_sync"] is None
    assert out["l3rt/svc-a"]["service_sync_mode"] == "skip"
    assert out["l3rt/svc-a"]["system_status_basis"] == "endpoint_fleet_sync"
    assert out["l3rt/svc-b"]["system_status_basis"] == "endpoint_fleet_sync"


@pytest.mark.asyncio
async def test_skip_mode_unknown_when_endpoint_sync_fails(monkeypatch):
    async def fake_mcp(client, tool, args=None):
        raise AssertionError("MCP should not be called in skip mode")

    monkeypatch.setattr("nso_facts.service_collect.call_mcp", fake_mcp)

    services_by_type = {
        "l3rt": {
            "status": "success",
            "data": {
                "services": [{"name": "svc-x", "device": "gpn-data-sw"}]
            },
        }
    }
    out = await collect_service_health(
        _FakeClient(),
        services_by_type,
        {"l3rt": "l3rt"},
        {"gpn-data-sw": "error: sync check failed"},
        service_sync_mode="skip",
    )
    assert out["l3rt/svc-x"]["status"] == "unknown"
    assert out["l3rt/svc-x"]["system_status_basis"] == "endpoint_fleet_sync"


@pytest.mark.asyncio
async def test_check_mode_still_calls_service_sync(monkeypatch):
    calls: list[str] = []

    async def fake_mcp(client, tool, args=None):
        calls.append(tool)
        if tool == "check_service_sync":
            return {
                "status": "success",
                "data": {"in_sync": True, "details": {}},
            }
        raise AssertionError(f"unexpected tool {tool}")

    monkeypatch.setattr("nso_facts.service_collect.call_mcp", fake_mcp)

    services_by_type = {
        "l3rt": {
            "status": "success",
            "data": {"services": [{"name": "svc-y", "device": "a"}]},
        }
    }
    out = await collect_service_health(
        _FakeClient(),
        services_by_type,
        {"l3rt": "l3rt"},
        {"a": "in-sync"},
        service_sync_mode="check",
    )
    assert calls == ["check_service_sync"]
    assert out["l3rt/svc-y"]["in_sync"] is True
    assert out["l3rt/svc-y"].get("service_sync_mode", "check") == "check"
    assert "system_status_basis" not in out["l3rt/svc-y"] or out[
        "l3rt/svc-y"
    ].get("system_status_basis") != "endpoint_fleet_sync"


def test_skip_note_constant():
    assert SERVICE_SYNC_SKIP_NOTE == (
        "SystemUp is a baseline from endpoint fleet sync (service sync "
        "skipped) — not fleet-wide dataplane verification. No dig or an "
        "incomplete dig keeps SystemUp; dig-confirmed down or degraded demotes."
    )


def test_skip_note_appears_under_services_table():
    from diagnostic_mas.case import Budget, CaseFile, add_evidence
    from diagnostic_mas.report import render_report

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "operational_summary": {
                    "l3rt": {"up": 1, "down": 0, "degraded": 0, "unknown": 0}
                },
                "extra": {
                    "services": {
                        "l3rt/svc-a": {
                            "name": "svc-a",
                            "service_type": "l3rt",
                            "status": "up",
                            "system_status": "up",
                        }
                    },
                    "service_sync_note": SERVICE_SYNC_SKIP_NOTE,
                },
            },
        },
    )
    text = render_report(case)
    assert "| l3rt" in text
    assert SERVICE_SYNC_SKIP_NOTE in text
    assert text.index("| l3rt") < text.index(SERVICE_SYNC_SKIP_NOTE)
    assert text.index(SERVICE_SYNC_SKIP_NOTE) < text.index(
        "received additional dataplane investigation"
    )
    assert "**Service sync:**" not in text
    assert text.index("## Run details") > text.index(SERVICE_SYNC_SKIP_NOTE)
