"""Tests for service type normalization."""

from agent.collect import _extract_service_type_names, normalize_service_type


def test_normalize_strips_ncs_services_prefix():
    assert normalize_service_type("/ncs:services/port-mirror:port-mirror") == "port-mirror"
    assert normalize_service_type("ncs:services/l3vpn:l3vpn") == "l3vpn"


def test_normalize_extracts_service_name():
    assert normalize_service_type("/ncs:services/esnet:l2vpn-eline") == "l2vpn-eline"


def test_normalize_bare_name_unchanged():
    assert normalize_service_type("port-mirror") == "port-mirror"


def test_extract_service_type_names_from_envelope():
    """MCP get_service_types returns {status, data:{service_types:[...]}}."""
    response = {
        "status": "success",
        "data": {
            "service_types": [
                {"name": "/ncs:services/l2ptp:l2ptp"},
                {"name": "/ncs:services/l3rt:l3rt"},
            ]
        },
    }
    assert _extract_service_type_names(response) == [
        "/ncs:services/l2ptp:l2ptp",
        "/ncs:services/l3rt:l3rt",
    ]


def test_extract_service_type_names_legacy_raw_yang():
    response = {
        "tailf-ncs:service-type": [
            {"name": "/ncs:services/l2bridge:l2bridge"},
        ]
    }
    assert _extract_service_type_names(response) == [
        "/ncs:services/l2bridge:l2bridge",
    ]


def test_extract_service_type_names_error_envelope():
    response = {
        "status": "error",
        "error_message": "401 Unauthorized",
        "error_code": "nso_error",
    }
    assert _extract_service_type_names(response) == []
