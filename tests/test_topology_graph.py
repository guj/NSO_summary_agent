"""Tests for topology graph helpers."""

from agent.topology.graph import (
    interface_edge_id,
    service_edge_id,
    summarize_issues,
    summarize_operational_physical,
    summarize_operational_services,
    summarize_static_physical,
    summarize_static_services,
)


def test_interface_edge_id():
    assert interface_edge_id("pe-den1", "Gi0/0/1") == "if:pe-den1:Gi0/0/1"


def test_summarize_static_physical():
    edges = [
        {"id": "if:a:Lo0", "local": {"device": "a", "interface": "Lo0"}},
        {"id": "if:a:Gi0", "local": {"device": "a", "interface": "Gi0"}},
        {"id": "if:b:Lo0", "local": {"device": "b", "interface": "Lo0"}},
    ]
    assert summarize_static_physical(edges) == {
        "total": 3,
        "by_device": {"a": 2, "b": 1},
    }


def test_summarize_operational_physical():
    edges = [
        {"id": "if:a:Lo0", "state": {"status": "up"}},
        {"id": "if:a:Gi0", "state": {"status": "down"}},
        {"id": "if:b:Gi1", "state": {"status": "degraded"}},
        {"id": "if:b:Gi2", "state": {"status": "unknown"}},
        {"id": "if:b:Gi3", "state": {"status": "admin-down"}},
    ]
    assert summarize_operational_physical(edges) == {
        "total": 5,
        "up": 1,
        "down": 1,
        "admin-down": 1,
        "degraded": 1,
        "unknown": 1,
        "by_device": {
            "a": {
                "total": 2,
                "up": 1,
                "down": 1,
                "admin-down": 0,
                "degraded": 0,
                "unknown": 0,
            },
            "b": {
                "total": 3,
                "up": 0,
                "down": 0,
                "admin-down": 1,
                "degraded": 1,
                "unknown": 1,
            },
        },
    }


def test_summarize_issues():
    issues = [
        {"layer": "physical", "code": "x"},
        {"layer": "physical", "code": "y"},
        {"layer": "routing", "code": "z"},
    ]
    summary = summarize_issues(issues)
    assert summary["issues_total"] == 3
    assert summary["issues_by_layer"]["physical"] == 2
    assert summary["issues_by_layer"]["routing"] == 1


def test_service_edge_id_orders_devices():
    assert (
        service_edge_id("l2ptp", "foo", "pe-den1", "pe-chi1")
        == "svc:l2ptp:foo:pe-chi1:pe-den1"
    )
    assert (
        service_edge_id("l2ptp", "foo", "pe-chi1", "pe-den1")
        == "svc:l2ptp:foo:pe-chi1:pe-den1"
    )


def test_service_edge_id_local():
    assert (
        service_edge_id("l2bridge", "bar", "sw1", None)
        == "svc:l2bridge:bar:sw1:_local"
    )


def test_summarize_static_services():
    edges = [
        {"meta": {"service_type": "l2ptp"}},
        {"meta": {"service_type": "l2ptp"}},
        {"meta": {"service_type": "l2bridge"}},
    ]
    assert summarize_static_services(edges) == {
        "total": 3,
        "by_type": {"l2bridge": 1, "l2ptp": 2},
    }


def test_summarize_operational_services():
    edges = [
        {"state": {"status": "up"}},
        {"state": {"status": "down"}},
        {"state": {"status": "degraded"}},
        {"state": {"status": "unknown"}},
    ]
    assert summarize_operational_services(edges) == {
        "total": 4,
        "up": 1,
        "down": 1,
        "degraded": 1,
        "unknown": 1,
    }
