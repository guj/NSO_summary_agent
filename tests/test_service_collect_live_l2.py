"""Tests for live L2 probing inside collect_service_health."""

from __future__ import annotations

import pytest

from nso_facts.service_collect import collect_service_health

RENC_DN = """
evpn_vpws  evpn_vpws_9001
                      DN   Hu0/0/0/0.100          UP       EVPN 9003,99,None      DN
----------------------------------------------------------------------------------------
"""

LBNL_UP = """
evpn_vpws  evpn_vpws_9001
                      UP   Hu0/0/0/17.100         UP       EVPN 9003,100,10.128.0.1
                                                                                  UP
----------------------------------------------------------------------------------------
"""


class _FakeClient:
    pass


@pytest.mark.asyncio
async def test_collect_marks_l2ptp_degraded_from_xconnect(monkeypatch):
    async def fake_mcp(client, tool, args=None):
        assert tool == "exec_show"
        device = (args or {}).get("device_name")
        text = LBNL_UP if device == "lbnl-data-sw" else RENC_DN
        return {
            "status": "success",
            "data": {"device": device, "command": "show l2vpn xconnect", "result": text},
        }

    monkeypatch.setattr("nso_facts.service_collect.call_mcp", fake_mcp)

    # Bypass check_service_sync by making it return null sync via our collect path —
    # collect_service_health calls check_service_sync then may call exec_show.
    calls: list[str] = []

    async def fake_mcp_all(client, tool, args=None):
        calls.append(tool)
        if tool == "check_service_sync":
            return {
                "status": "success",
                "data": {"in_sync": None, "outcome": "failed", "details": {}},
            }
        if tool == "exec_show":
            device = (args or {}).get("device_name")
            text = LBNL_UP if device == "lbnl-data-sw" else RENC_DN
            return {
                "status": "success",
                "data": {
                    "device": device,
                    "command": "show l2vpn xconnect",
                    "result": text,
                },
            }
        raise AssertionError(f"unexpected tool {tool}")

    monkeypatch.setattr("nso_facts.service_collect.call_mcp", fake_mcp_all)

    instance = {
        "name": "l2-PTP-broken",
        "stp-a": {
            "device": "lbnl-data-sw",
            "interface": {"type": "HundredGigE", "id": "0/0/0/17", "outervlan": 100},
        },
        "stp-z": {
            "device": "renc-data-sw",
            "interface": {"type": "HundredGigE", "id": "0/0/0/0", "outervlan": 100},
        },
    }
    services_by_type = {
        "l2ptp": {"status": "success", "data": {"services": [instance]}},
    }
    out = await collect_service_health(
        _FakeClient(),
        services_by_type,
        {"l2ptp": "l2ptp"},
        {"lbnl-data-sw": "in-sync", "renc-data-sw": "in-sync"},
    )
    rec = out["l2ptp/l2-PTP-broken"]
    assert rec["status"] == "degraded"
    assert rec["live_l2"]["summary"] == "degraded"
    assert "exec_show" in calls
