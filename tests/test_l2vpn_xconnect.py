"""Tests for L2VPN xconnect parse / AC match / live summary."""

from __future__ import annotations

from nso_facts.l2vpn_xconnect import (
    ac_name_from_endpoint,
    extract_l2_access_endpoints,
    parse_l2vpn_xconnect,
    summarize_live_l2,
)

RENC_XCONNECT = """
Wed Aug 12 17:04:40.686 UTC
Legend: ST = State, UP = Up, DN = Down

XConnect                   Segment 1                       Segment 2
Group      Name       ST   Description            ST       Description            ST
------------------------   -----------------------------   -----------------------------
evpn_vpws  evpn_vpws_9001
                      DN   Hu0/0/0/0.100          UP       EVPN 9003,99,None      DN
----------------------------------------------------------------------------------------
evpn_vpws  evpn_vpws_9002
                      UP   Hu0/0/0/0.1002         UP       EVPN 9002,1001,10.129.0.1
                                                                                  UP
----------------------------------------------------------------------------------------
"""

LBNL_XCONNECT = """
evpn_vpws  evpn_vpws_9001
                      UP   Hu0/0/0/17.100         UP       EVPN 9003,100,10.128.0.1
                                                                                  UP
----------------------------------------------------------------------------------------
evpn_vpws  evpn_vpws_9002
                      UP   Hu0/0/0/9.1001         UP       EVPN 9002,1002,10.128.0.1
                                                                                  UP
----------------------------------------------------------------------------------------
"""


def test_parse_l2vpn_xconnect_rows():
    rows = parse_l2vpn_xconnect(RENC_XCONNECT)
    by_ac = {r["ac"]: r for r in rows}
    assert by_ac["Hu0/0/0/0.100"]["st"] == "DN"
    assert by_ac["Hu0/0/0/0.100"]["name"] == "evpn_vpws_9001"
    assert by_ac["Hu0/0/0/0.1002"]["st"] == "UP"


def test_ac_name_from_endpoint_with_vlan():
    assert (
        ac_name_from_endpoint(
            {"type": "HundredGigE", "id": "0/0/0/17", "outervlan": 100}
        )
        == "Hu0/0/0/17.100"
    )


def test_extract_l2_access_endpoints_from_l2ptp():
    instance = {
        "name": "l2-PTP-89dc8418-358d-403c-b359-0b1c01905d6f",
        "stp-a": {
            "device": "lbnl-data-sw",
            "interface": {"type": "HundredGigE", "id": "0/0/0/17", "outervlan": 100},
        },
        "stp-z": {
            "device": "renc-data-sw",
            "interface": {"type": "HundredGigE", "id": "0/0/0/0", "outervlan": 100},
        },
    }
    eps = extract_l2_access_endpoints(instance)
    assert eps == [
        {"device": "lbnl-data-sw", "ac": "Hu0/0/0/17.100"},
        {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100"},
    ]


def test_summarize_live_l2_degraded_when_one_side_dn():
    lbnl = parse_l2vpn_xconnect(LBNL_XCONNECT)
    renc = parse_l2vpn_xconnect(RENC_XCONNECT)
    by_dev = {"lbnl-data-sw": lbnl, "renc-data-sw": renc}
    endpoints = [
        {"device": "lbnl-data-sw", "ac": "Hu0/0/0/17.100"},
        {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100"},
    ]
    live = summarize_live_l2(endpoints, by_dev)
    assert live["summary"] == "degraded"
    assert any(e.get("st") == "DN" for e in live["endpoints"])


def test_summarize_live_l2_up_when_both_up():
    lbnl = parse_l2vpn_xconnect(LBNL_XCONNECT)
    renc = parse_l2vpn_xconnect(RENC_XCONNECT)
    by_dev = {"lbnl-data-sw": lbnl, "renc-data-sw": renc}
    endpoints = [
        {"device": "lbnl-data-sw", "ac": "Hu0/0/0/9.1001"},
        {"device": "renc-data-sw", "ac": "Hu0/0/0/0.1002"},
    ]
    live = summarize_live_l2(endpoints, by_dev)
    assert live["summary"] == "up"


def test_summarize_live_l2_unknown_when_device_missing():
    live = summarize_live_l2(
        [{"device": "sw1", "ac": "Hu0/0/0/1.100"}],
        {},
    )
    assert live["summary"] == "unknown"
