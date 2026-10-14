"""Tests for IOS-XR interface name normalization."""

from nso_facts.topology.interfaces import (
    canonical_interface_name,
    expand_interface_name,
    interface_variants,
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


def test_a_long_name_is_not_read_as_an_abbreviation():
    # "FourHundredGigE" begins with "Fo", the short form of FortyGigE; "HundredGigE" with its own "Hu".
    assert interface_variants("FourHundredGigE0/0/0/26") == {"FourHundredGigE0/0/0/26", "FH0/0/0/26"}
    assert interface_variants("TenGigE0/0/0/18/0") == {"TenGigE0/0/0/18/0", "Te0/0/0/18/0"}
    assert expand_interface_name("FourHundredGigE0/0/0/26") == "FourHundredGigE0/0/0/26"
    assert expand_interface_name("HundredGigE0/0/0/23") == "HundredGigE0/0/0/23"
    assert expand_interface_name("Bundle-Ether100") == "Bundle-Ether100"


def test_only_bvi_loopback_and_null_have_no_port_of_their_own():
    from nso_facts.topology.interfaces import has_own_port

    for name in ("BVI4219", "BV4219", "Loopback0", "Lo0", "Null0", "Nu0"):
        assert not has_own_port(name), name
    # Ethernet ports, breakout lanes and bundles can be inspected; so can any type the table does not know.
    for name in ("HundredGigE0/0/0/14", "Hu0/0/0/14", "TwentyFiveGigE0/0/0/10/2", "FourHundredGigE0/0/0/26",
                 "Bundle-Ether100", "BE100", "MgmtEth0/RP0/CPU0/0", "tunnel-te1"):
        assert has_own_port(name), name


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
