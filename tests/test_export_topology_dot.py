"""Tests for Graphviz topology export."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_export_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "export_topology_dot.py"
    spec = importlib.util.spec_from_file_location("export_topology_dot", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_services_pair_and_self_loop():
    mod = _load_export_module()
    topo = {
        "layers": {
            "services": {
                "edges": [
                    {
                        "type": "service_endpoint_pair",
                        "local": {"device": "a"},
                        "remote": {"device": "b"},
                        "meta": {"service_type": "l2ptp", "name": "foo"},
                    },
                    {
                        "type": "service_endpoint_pair",
                        "local": {"device": "sw1"},
                        "remote": None,
                        "meta": {"service_type": "l2bridge", "name": "bar"},
                    },
                ]
            }
        }
    }
    dot = mod.edges_to_dot(topo, layers=("services",), include_unlinked=False)
    assert '"a" -> "b"' in dot
    assert "l2ptp/foo" in dot
    assert '"sw1" -> "sw1"' in dot
    assert "l2bridge/bar" in dot
