"""Tests for IOS-XR interface name normalization."""

from agent.topology.interfaces import (
    canonical_interface_name,
    expand_interface_name,
    interfaces_match,
    physical_interfaces_by_device,
)


def test_interfaces_match_hu_hundredgige():
    assert interfaces_match("Hu0/0/0/0.2401", "HundredGigE0/0/0/0.2401")


def test_interfaces_match_fo_fortygige():
    assert interfaces_match("Fo0/0/0/28.3981", "FortyGigE0/0/0/28.3981")


def test_interfaces_match_fh_fourhundredgige():
    assert interfaces_match("FH0/0/0/25", "FourHundredGigE0/0/0/25")


def test_interfaces_match_tf_twentyfivegige():
    assert interfaces_match("TF0/0/0/10/0", "TwentyFiveGigE0/0/0/10/0")


def test_interfaces_match_bv_bvi():
    assert interfaces_match("BV4002", "BVI4002")


def test_interfaces_match_lo_loopback():
    assert interfaces_match("Lo0", "Loopback0")


def test_interfaces_match_be_bundle_ether():
    assert interfaces_match("BE100", "Bundle-Ether100")


def test_interfaces_match_nu_null():
    assert interfaces_match("Nu0", "Null0")


def test_expand_interface_name():
    assert expand_interface_name("Hu0/0/0/0.2401") == "HundredGigE0/0/0/0.2401"
    assert expand_interface_name("TF0/0/0/10/0") == "TwentyFiveGigE0/0/0/10/0"
    assert expand_interface_name("BV4002") == "BVI4002"


def test_canonical_interface_name_from_physical():
    physical = [
        {
            "local": {
                "device": "lbnl-data-sw",
                "interface": "HundredGigE0/0/0/0.2401",
            }
        }
    ]
    by_device = physical_interfaces_by_device(physical)
    assert (
        canonical_interface_name("lbnl-data-sw", "Hu0/0/0/0.2401", by_device)
        == "HundredGigE0/0/0/0.2401"
    )
