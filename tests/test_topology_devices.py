"""Tests for device inventory parsing."""

from agent.topology.devices import parse_device_names


def test_parse_device_names_from_mcp_success_wrapper():
    response = {
        "status": "success",
        "data": {
            "devices": [
                {"name": "uky-data-sw"},
                {"name": "renc-data-sw"},
            ]
        },
    }
    assert parse_device_names(response) == ["renc-data-sw", "uky-data-sw"]


def test_parse_device_names_from_tailf_key():
    response = {"tailf-ncs:device": [{"name": "core-1"}, {"name": "core-2"}]}
    assert parse_device_names(response) == ["core-1", "core-2"]
