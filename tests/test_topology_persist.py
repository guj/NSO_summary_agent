"""Tests for static topology persistence."""

import json
from pathlib import Path

from agent.topology.persist import (
    build_static_document,
    load_static_file,
    merge_services_layer,
    save_static_file,
    static_rebuild_needed,
)


def test_static_rebuild_needed_when_missing(tmp_path: Path):
    rebuild, reason = static_rebuild_needed(tmp_path, ["a", "b"])
    assert rebuild is True
    assert reason == "initial"


def test_static_rebuild_needed_when_devices_change(tmp_path: Path):
    doc = build_static_document(
        device_names=["a"],
        physical_edges=[],
        underlay_edges=[],
        build_reason="initial",
    )
    save_static_file(tmp_path, doc)

    rebuild, reason = static_rebuild_needed(tmp_path, ["a", "b"])
    assert rebuild is True
    assert reason == "device_added"


def test_static_rebuild_skipped_when_cache_valid(tmp_path: Path):
    doc = build_static_document(
        device_names=["a", "b"],
        physical_edges=[{"id": "if:a:Lo0", "type": "interface"}],
        underlay_edges=[],
        build_reason="initial",
    )
    save_static_file(tmp_path, doc)

    rebuild, reason = static_rebuild_needed(tmp_path, ["a", "b"])
    assert rebuild is False
    assert reason == "cache"


def test_save_and_load_static_roundtrip(tmp_path: Path):
    doc = build_static_document(
        device_names=["sw1"],
        physical_edges=[
            {
                "id": "if:sw1:Loopback0",
                "type": "interface",
                "local": {"device": "sw1", "interface": "Loopback0"},
                "remote": None,
            }
        ],
        underlay_edges=[],
        build_reason="initial",
    )
    save_static_file(tmp_path, doc)
    loaded = load_static_file(tmp_path)
    assert loaded is not None
    assert loaded["nodes"] == [{"id": "sw1", "site_id": None}]
    assert loaded["layers"]["physical"]["summary"]["total"] == 1
    assert loaded["layers"]["services"]["summary"] == {"total": 0, "by_type": {}}
    assert json.loads(json.dumps(loaded)) == loaded


def test_merge_services_layer(tmp_path: Path):
    doc = build_static_document(
        device_names=["a", "b"],
        physical_edges=[],
        underlay_edges=[],
        build_reason="initial",
    )
    edges = [
        {
            "id": "svc:l2ptp:foo:a:b",
            "type": "service_endpoint_pair",
            "local": {"device": "a"},
            "remote": {"device": "b"},
            "meta": {"service_type": "l2ptp", "name": "foo"},
        }
    ]
    merge_services_layer(doc, edges)
    assert doc["layers"]["services"]["summary"]["total"] == 1
    assert doc["layers"]["services"]["edges"][0]["id"] == "svc:l2ptp:foo:a:b"
    save_static_file(tmp_path, doc)
    loaded = load_static_file(tmp_path)
    assert loaded is not None
    assert loaded["layers"]["services"]["summary"]["by_type"] == {"l2ptp": 1}
