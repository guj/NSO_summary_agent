"""Every link the report reads names its two ends; one that cannot be named is reported, never dropped quietly."""
import pytest

from nso_facts import fleet_rollups
from nso_facts.fleet_rollups import merged_edges
from nso_facts.topology import graph
from nso_facts.topology.graph import bgp_edge_id, interface_edge_id, isis_edge_id

ISIS = isis_edge_id("amst-data-sw", "HundredGigE0/0/0/23.2060", "cern-data-sw", "Bundle-Ether100.2060")


def test_each_id_format_the_code_builds_names_its_ends():
    assert graph.ends_from_edge_id(ISIS) == (
        {"device": "amst-data-sw", "interface": "HundredGigE0/0/0/23.2060"},
        {"device": "cern-data-sw", "interface": "Bundle-Ether100.2060"})
    assert graph.ends_from_edge_id(bgp_edge_id("dall-data-sw", "10.133.128.1", "cien-data-sw", "10.148.0.1")) == (
        {"device": "dall-data-sw", "address": "10.133.128.1"},
        {"device": "cien-data-sw", "address": "10.148.0.1"})
    assert graph.ends_from_edge_id(interface_edge_id("fiu-data-sw", "HundredGigE0/0/0/24/2")) == (
        {"device": "fiu-data-sw", "interface": "HundredGigE0/0/0/24/2"}, None)


@pytest.mark.parametrize("edge_id", [
    "bgp:2001:db8::1:2001:db8::2:a-data-sw:b-data-sw",   # IPv6 addresses do not split cleanly
    "isis:a-data-sw:Hu0/0/0/1",                           # too few parts
    "bgp-outside:wash-data-sw:10.0.0.1",                  # not a format with two named ends
    "if::Hu0/0/0/1",                                      # no device
    None,
])
def test_an_id_that_does_not_split_cleanly_names_no_ends(edge_id):
    assert graph.ends_from_edge_id(edge_id) is None


def test_a_link_without_a_static_twin_is_kept_with_ends_from_its_id():
    layers = {"underlay": {"edges": [{"id": ISIS, "state": {"local": "up", "remote": "up", "status": "up"}}]}}

    [link] = merged_edges(layers, {"underlay": {"edges": []}}, "underlay")

    assert link["local"]["device"] == "amst-data-sw" and link["remote"]["device"] == "cern-data-sw"
    assert link["state"]["status"] == "up"


def test_a_link_that_cannot_be_named_is_reported_not_dropped_quietly(capsys, monkeypatch):
    monkeypatch.setattr(fleet_rollups, "_unnamed_reported", set())
    layers = {"routing": {"edges": [{"id": "bgp:2001:db8::1:2001:db8::2:a-data-sw:b-data-sw", "state": {}}]}}

    assert merged_edges(layers, {}, "routing") == []
    merged_edges(layers, {}, "routing")

    err = capsys.readouterr().err
    assert err.count("could not name the ends of routing link bgp:2001:db8::1") == 1
