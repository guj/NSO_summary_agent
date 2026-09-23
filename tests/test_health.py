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


def test_classify_unknown_when_device_unreachable():
    """NED/device unreachable on sync check is a query failure → unknown, not down."""
    sync = {"status": "success", "data": {"in_sync": True}}
    assert classify_instance(sync, ["error: device unreachable"]) == "unknown"


def test_classify_unknown_when_device_sync_timed_out():
    """NSO/API sync timeouts are verification gaps, not confirmed down."""
    sync = {"status": "success", "data": {"in_sync": True}}
    timeout = (
        "error: HTTPSConnectionPool(host='192.168.11.246', port=443): "
        "Read timed out. (read timeout=10)"
    )
    assert classify_instance(sync, ["in-sync", timeout, "in-sync"]) == "unknown"
    assert classify_instance(sync, [timeout]) == "unknown"


def test_classify_unknown_when_device_sync_api_error():
    sync = {"status": "success", "data": {"in_sync": None}}
    assert (
        classify_instance(sync, ["error: RESTCONF 500 at https://nso/restconf"])
        == "unknown"
    )


def test_classify_unknown_when_sync_result_times_out():
    sync = {
        "status": "error",
        "error_message": "Read timed out. (read timeout=10)",
    }
    assert classify_instance(sync, ["in-sync"]) == "unknown"


def test_classify_unknown_when_in_sync_none_and_no_device_results():
    sync = {"status": "success", "data": {"in_sync": None}}
    assert classify_instance(sync, []) == "unknown"


def test_classify_unknown_when_sync_missing():
    assert classify_instance(None, []) == "unknown"


def test_parse_in_sync_from_result_string():
    sync = {
        "status": "success",
        "data": {"in_sync": None, "details": {"result": "in-sync"}},
    }
    assert parse_in_sync(sync) is True


def test_parse_in_sync_from_sync_state():
    sync = {"status": "success", "data": {"sync_state": "in-sync"}}
    assert parse_in_sync(sync) is True
    assert classify_instance(sync, []) == "up"


def test_classify_up_from_result_string_when_devices_in_sync():
    sync = {
        "status": "success",
        "data": {"in_sync": None, "details": {"result": "in-sync"}},
    }
    assert classify_instance(sync, ["in-sync"]) == "up"


def test_classify_up_from_device_sync_when_service_sync_missing():
    assert classify_instance(None, ["in-sync", "in-sync"]) == "up"


def test_classify_degraded_when_live_l2_asymmetric():
    # live_l2 no longer affects hardcoded overall status
    assert (
        classify_instance(None, ["in-sync", "in-sync"], live_l2_summary="degraded")
        == "up"
    )


def test_classify_down_when_live_l2_down():
    assert (
        classify_instance(
            {"status": "success", "data": {"in_sync": True}},
            ["in-sync"],
            live_l2_summary="down",
        )
        == "up"
    )


def test_classify_keeps_sync_up_when_live_l2_up():
    assert (
        classify_instance(None, ["in-sync", "in-sync"], live_l2_summary="up") == "up"
    )


def test_layered_status_collector_leaves_dataplane_not_checked():
    from nso_facts.health import (
        apply_dataplane_status,
        build_instance_record,
        combine_service_status,
    )

    sync = {"status": "success", "data": {"in_sync": True}}
    live = {
        "probed": True,
        "summary": "down",
        "endpoints": [{"device": "a", "ac": "Hu0/0/0/1.100", "error": "ac_not_found"}],
    }
    rec = build_instance_record(
        "l2sts",
        {"name": "svc", "site-a": {"device": "a"}, "site-z": {"device": "b"}},
        sync,
        {"a": "in-sync", "b": "in-sync"},
        live_l2=live,
    )
    assert rec["system_status"] == "up"
    assert rec["dataplane_status"] == "not_checked"
    assert rec["status"] == "up"
    assert rec["live_l2"]["summary"] == "down"  # evidence only
    apply_dataplane_status(rec, "down")
    assert rec["dataplane_status"] == "down"
    assert rec["status"] == "down"
    assert combine_service_status("up", "not_checked") == "up"


def test_dataplane_not_checked_does_not_override_system_up():
    from nso_facts.health import combine_service_status, classify_dataplane_status

    assert classify_dataplane_status(None) == "not_checked"
    assert combine_service_status("up", "not_checked") == "up"


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
    # live_l2 is evidence only; overall stays system until LLM dataplane
    assert record["system_status"] == "up"
    assert record["dataplane_status"] == "not_checked"
    assert record["status"] == "up"
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


def test_build_instance_record_timeout_is_unknown_not_down():
    """Sync timeout-only → unknown; counts bucket under unknown."""
    timeout = (
        "error: HTTPSConnectionPool(host='192.168.11.246', port=443): "
        "Read timed out. (read timeout=10)"
    )
    record = build_instance_record(
        "l3rt",
        {"name": "svc-timeout", "device": "star-data-sw"},
        {"status": "success", "data": {"in_sync": True}},
        {"star-data-sw": timeout},
    )
    assert record["system_status"] == "unknown"
    assert record["status"] == "unknown"
    assert record["status"] != "down"

    counts = derive_counts(
        {"l3rt": {"status": "success", "data": {"services": [{}]}}},
        {"l3rt/svc-timeout": record},
    )
    assert counts["l3rt"]["down"] == 0
    assert counts["l3rt"]["unknown"] == 1


def test_dataplane_down_overrides_system_unknown():
    """Positive live path failure can still mark the service Down."""
    from nso_facts.health import apply_dataplane_status

    record = {
        "system_status": "unknown",
        "dataplane_status": "not_checked",
        "status": "unknown",
    }
    apply_dataplane_status(record, "down")
    assert record["dataplane_status"] == "down"
    assert record["status"] == "down"


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
