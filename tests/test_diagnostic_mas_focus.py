from diagnostic_mas.focus import (
    counts_from_services,
    filter_device_names,
    filter_services,
    resolve_spine_flags,
)


def test_resolve_spine_flags_modes():
    assert resolve_spine_flags(isis_only=True) == (True, False, False, False)
    assert resolve_spine_flags(bgp_only=True) == (False, True, False, False)
    assert resolve_spine_flags(device_only=True) == (False, False, False, True)
    assert resolve_spine_flags(service_only=True) == (False, False, True, False)
    assert resolve_spine_flags(service_focus=True) == (False, False, True, False)
    assert resolve_spine_flags() == (True, True, True, False)
    assert resolve_spine_flags(skip_service=True) == (True, True, False, False)


def test_filter_device_names_exact_and_substring():
    names = ["renc-data-sw", "lbnl-data-sw", "uky-data-sw"]
    assert filter_device_names(names, "renc-data-sw") == ["renc-data-sw"]
    assert filter_device_names(names, "renc,lbnl") == [
        "renc-data-sw",
        "lbnl-data-sw",
    ]
    assert filter_device_names(names, "") == names


def test_filter_services_by_type_id_device():
    services = {
        "l2ptp/a": {
            "name": "a",
            "service_type": "l2ptp",
            "status": "up",
            "devices": ["renc-data-sw"],
        },
        "l2ptp/b": {
            "name": "b",
            "service_type": "l2ptp",
            "status": "down",
            "devices": ["lbnl-data-sw"],
        },
        "l3rt/c": {
            "name": "c",
            "service_type": "l3rt",
            "status": "up",
            "devices": ["renc-data-sw"],
        },
    }
    by_type = filter_services(services, service_type="l2ptp")
    assert set(by_type) == {"l2ptp/a", "l2ptp/b"}
    by_id = filter_services(services, service_id="b")
    assert set(by_id) == {"l2ptp/b"}
    by_dev = filter_services(services, devices=["renc-data-sw"])
    assert set(by_dev) == {"l2ptp/a", "l3rt/c"}
    combo = filter_services(
        services, service_type="l2ptp", devices=["renc-data-sw"]
    )
    assert set(combo) == {"l2ptp/a"}


def test_counts_from_filtered_services():
    services = {
        "l2ptp/a": {"service_type": "l2ptp", "status": "up"},
        "l2ptp/b": {"service_type": "l2ptp", "status": "degraded"},
    }
    counts = counts_from_services(services)
    assert counts["l2ptp"]["up"] == 1
    assert counts["l2ptp"]["degraded"] == 1
