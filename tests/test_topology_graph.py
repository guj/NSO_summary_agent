"""Tests for topology graph helpers."""

from nso_facts.topology.graph import (
    interface_edge_id,
    summarize_operational_physical,
    summarize_static_physical,
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
