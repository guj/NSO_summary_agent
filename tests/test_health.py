"""Tests for per-instance health classification."""

from agent.health import (
    build_device_sync_map,
    build_instance_record,
    classify_instance,
    derive_counts,
    extract_devices,
    parse_in_sync,
    sync_module_from_type,
)


def test_classify_up_when_service_and_device_in_sync():
    sync = {"status": "success", "data": {"in_sync": True}}
    assert classify_instance(sync, ["in-sync"]) == "up"


def test_classify_degraded_when_service_out_of_sync():
    sync = {"status": "success", "data": {"in_sync": False}}
    assert classify_instance(sync, ["in-sync"]) == "degraded"


def test_classify_degraded_when_device_out_of_sync():
    sync = {"status": "success", "data": {"in_sync": True}}
    assert classify_instance(sync, ["out-of-sync"]) == "degraded"


def test_classify_down_when_device_unreachable():
    sync = {"status": "success", "data": {"in_sync": True}}
    assert classify_instance(sync, ["error: device unreachable"]) == "down"


def test_classify_unknown_when_sync_missing():
    assert classify_instance(None, []) == "unknown"


def test_parse_in_sync_from_result_string():
    sync = {
        "status": "success",
        "data": {"in_sync": None, "details": {"result": "in-sync"}},
    }
    assert parse_in_sync(sync) is True


def test_classify_up_from_result_string_when_devices_in_sync():
    sync = {
        "status": "success",
        "data": {"in_sync": None, "details": {"result": "in-sync"}},
    }
    assert classify_instance(sync, ["in-sync"]) == "up"


def test_classify_up_from_device_sync_when_service_sync_missing():
    assert classify_instance(None, ["in-sync", "in-sync"]) == "up"


def test_classify_degraded_when_live_l2_asymmetric():
    assert (
        classify_instance(None, ["in-sync", "in-sync"], live_l2_summary="degraded")
        == "degraded"
    )


def test_classify_down_when_live_l2_down():
    assert (
        classify_instance(
            {"status": "success", "data": {"in_sync": True}},
            ["in-sync"],
            live_l2_summary="down",
        )
        == "down"
    )


def test_classify_keeps_sync_up_when_live_l2_up():
    assert (
        classify_instance(None, ["in-sync", "in-sync"], live_l2_summary="up") == "up"
    )


def test_build_instance_record_attaches_live_l2():
    live = {
        "probed": True,
        "summary": "degraded",
        "endpoints": [{"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "DN"}],
    }
    record = build_instance_record(
        "l2ptp",
        {"name": "svc", "stp-a": {"device": "a"}, "stp-z": {"device": "b"}},
        None,
        {"a": "in-sync", "b": "in-sync"},
        live_l2=live,
    )
    assert record["status"] == "degraded"
    assert record["live_l2"]["summary"] == "degraded"


def test_sync_module_from_type():
    assert sync_module_from_type("/ncs:services/l3rt:l3rt") == "l3rt"
    assert sync_module_from_type("/ncs:services/esnet:l2vpn-eline") == "esnet"


def test_extract_devices_from_common_fields():
    assert extract_devices({"device": "core-1"}) == ["core-1"]
    assert extract_devices({"device-list": [{"device": "a"}, {"device": "b"}]}) == [
        "a",
        "b",
    ]


def test_extract_devices_from_l2ptp_nested_stp():
    instance = {
        "name": "fabric-l2ptp-test",
        "stp-a": {"device": "renc-data-sw", "interface": {"type": "HundredGigE"}},
        "stp-z": {"device": "uky-data-sw", "interface": {"type": "HundredGigE"}},
    }
    assert extract_devices(instance) == ["renc-data-sw", "uky-data-sw"]


def test_classify_l2ptp_up_when_devices_in_sync_and_service_sync_missing():
    instance = {
        "stp-a": {"device": "sw1"},
        "stp-z": {"device": "sw2"},
    }
    record = build_instance_record("l2ptp", instance, None, {"sw1": "in-sync", "sw2": "in-sync"})
    assert record["status"] == "up"
    assert record["devices"] == ["sw1", "sw2"]


def test_build_device_sync_map():
    fleet = {
        "status": "success",
        "data": {
            "devices": [
                {"device": "sw1", "result": "in-sync"},
                {"device": "sw2", "result": "out-of-sync"},
            ]
        },
    }
    assert build_device_sync_map(fleet) == {
        "sw1": "in-sync",
        "sw2": "out-of-sync",
    }


def test_build_instance_record():
    record = build_instance_record(
        "l3rt",
        {"name": "svc-1", "device": "sw1"},
        {"status": "success", "data": {"in_sync": True}},
        {"sw1": "in-sync"},
    )
    assert record["status"] == "up"
    assert record["in_sync"] is True
    assert record["device_sync"] == {"sw1": "in-sync"}


def test_derive_counts_from_services():
    services = {
        "l3rt/svc-1": {
            "service_type": "l3rt",
            "status": "up",
        },
        "idipa/svc-2": {
            "service_type": "idipa",
            "status": "degraded",
        },
        "idipa/svc-3": {
            "service_type": "idipa",
            "status": "up",
        },
    }
    services_by_type = {
        "l3rt": {"status": "success", "data": {"services": [{}]}},
        "idipa": {"status": "success", "data": {"services": [{}, {}]}},
    }
    counts = derive_counts(services_by_type, services)
    assert counts["l3rt"] == {
        "total": 1,
        "up": 1,
        "down": 0,
        "degraded": 0,
        "unknown": 0,
    }
    assert counts["idipa"] == {
        "total": 2,
        "up": 1,
        "down": 0,
        "degraded": 1,
        "unknown": 0,
    }
