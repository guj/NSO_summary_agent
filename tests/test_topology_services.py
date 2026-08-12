"""Tests for services topology layer builders."""

from agent.topology.services import (
    build_operational_service_edges,
    build_operational_services_layer,
    build_static_service_edges,
    build_static_services_layer,
)


def _ok_services(instances: list[dict]) -> dict:
    return {"status": "success", "data": {"services": instances}}


def test_static_two_device_pair():
    sbt = {
        "l2ptp": _ok_services(
            [
                {
                    "name": "foo",
                    "stp-a": {"device": "pe-den1"},
                    "stp-z": {"device": "pe-chi1"},
                }
            ]
        )
    }
    edges, issues = build_static_service_edges(sbt)
    assert issues == []
    assert len(edges) == 1
    e = edges[0]
    assert e["id"] == "svc:l2ptp:foo:pe-chi1:pe-den1"
    assert e["type"] == "service_endpoint_pair"
    assert e["local"] == {"device": "pe-chi1"}
    assert e["remote"] == {"device": "pe-den1"}
    assert e["meta"] == {"service_type": "l2ptp", "name": "foo"}


def test_static_three_device_all_pairs():
    sbt = {
        "mpls": _ok_services(
            [
                {
                    "name": "tri",
                    "a": {"device": "a"},
                    "b": {"device": "b"},
                    "c": {"device": "c"},
                }
            ]
        )
    }
    edges, _ = build_static_service_edges(sbt)
    ids = sorted(e["id"] for e in edges)
    assert ids == [
        "svc:mpls:tri:a:b",
        "svc:mpls:tri:a:c",
        "svc:mpls:tri:b:c",
    ]


def test_static_single_device_local():
    sbt = {"l2bridge": _ok_services([{"name": "bar", "device": "sw1"}])}
    edges, _ = build_static_service_edges(sbt)
    assert len(edges) == 1
    assert edges[0]["id"] == "svc:l2bridge:bar:sw1:_local"
    assert edges[0]["remote"] is None


def test_static_zero_devices_issue():
    sbt = {"l2ptp": _ok_services([{"name": "empty"}])}
    edges, issues = build_static_service_edges(sbt)
    assert edges == []
    assert len(issues) == 1
    assert issues[0]["code"] == "service_no_devices"
    assert issues[0]["layer"] == "services"
    assert issues[0]["severity"] == "low"
    assert issues[0].get("edge_id") is None


def test_prefer_devices_from_services_record():
    sbt = {
        "l2ptp": _ok_services(
            [
                {
                    "name": "foo",
                    "stp-a": {"device": "pe-den1"},
                    "stp-z": {"device": "pe-chi1"},
                }
            ]
        )
    }
    services = {
        "l2ptp/foo": {
            "service_type": "l2ptp",
            "name": "foo",
            "devices": ["x", "y"],
            "status": "up",
        }
    }
    edges, _ = build_static_service_edges(sbt, services=services)
    assert edges[0]["id"] == "svc:l2ptp:foo:x:y"


def test_operational_status_and_one_issue_per_instance():
    static = [
        {
            "id": "svc:mpls:tri:a:b",
            "type": "service_endpoint_pair",
            "local": {"device": "a"},
            "remote": {"device": "b"},
            "meta": {"service_type": "mpls", "name": "tri"},
        },
        {
            "id": "svc:mpls:tri:a:c",
            "type": "service_endpoint_pair",
            "local": {"device": "a"},
            "remote": {"device": "c"},
            "meta": {"service_type": "mpls", "name": "tri"},
        },
        {
            "id": "svc:mpls:tri:b:c",
            "type": "service_endpoint_pair",
            "local": {"device": "b"},
            "remote": {"device": "c"},
            "meta": {"service_type": "mpls", "name": "tri"},
        },
    ]
    services = {
        "mpls/tri": {
            "service_type": "mpls",
            "name": "tri",
            "devices": ["a", "b", "c"],
            "status": "down",
        }
    }
    op, issues = build_operational_service_edges(static, services)
    assert len(op) == 3
    assert all(e["state"]["status"] == "down" for e in op)
    assert len(issues) == 1
    assert issues[0]["code"] == "service_down"
    assert issues[0]["severity"] == "high"
    assert issues[0]["edge_id"] == "svc:mpls:tri:a:b"


def test_operational_up_no_issue():
    static = [
        {
            "id": "svc:l2ptp:foo:pe-chi1:pe-den1",
            "meta": {"service_type": "l2ptp", "name": "foo"},
            "local": {"device": "pe-chi1"},
            "remote": {"device": "pe-den1"},
        }
    ]
    services = {
        "l2ptp/foo": {"service_type": "l2ptp", "name": "foo", "status": "up"}
    }
    op, issues = build_operational_service_edges(static, services)
    assert op[0]["state"]["status"] == "up"
    assert issues == []


def test_operational_missing_record_unknown():
    static = [
        {
            "id": "svc:l2ptp:foo:a:b",
            "meta": {"service_type": "l2ptp", "name": "foo"},
            "local": {"device": "a"},
            "remote": {"device": "b"},
        }
    ]
    op, issues = build_operational_service_edges(static, {})
    assert op[0]["state"]["status"] == "unknown"
    assert issues[0]["code"] == "service_unknown"
    assert issues[0]["severity"] == "low"


def test_static_layer_helper_shapes():
    layer = build_static_services_layer([])
    assert layer == {"edges": [], "summary": {"total": 0, "by_type": {}}}
    op = build_operational_services_layer([])
    assert op["summary"]["total"] == 0
