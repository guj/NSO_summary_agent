"""Device Health Interfaces driven by multi-agent assembled topology issues."""

from __future__ import annotations

from multi_agent.base import AgentResult
from multi_agent.fleet_spine import assemble_topology
from nso_report.executive import build_device_health_rows


def test_assembled_topology_marks_inventory_review_from_bvi_mismatch():
    phys = [
        {
            "id": "if:lbnl-data-sw:BVI4005",
            "type": "interface",
            "local": {"device": "lbnl-data-sw", "interface": "BVI4005"},
        }
    ]
    topo = assemble_topology(
        device_names=["lbnl-data-sw"],
        physical_edges=phys,
        physical_op_edges=[
            {
                "id": "if:lbnl-data-sw:BVI4005",
                "type": "interface",
                "local": {
                    "device": "lbnl-data-sw",
                    "interface": "BVI4005",
                    "admin_status": "unknown",
                    "oper_status": "unknown",
                },
                "state": {"status": "unknown"},
            }
        ],
        isis=AgentResult(name="isis", layer="underlay"),
        bgp=AgentResult(name="bgp", layer="routing"),
        extra_issues=[
            {
                "severity": "low",
                "layer": "physical",
                "code": "config_live_mismatch",
                "edge_id": "if:lbnl-data-sw:BVI4005",
                "message": "lbnl-data-sw BVI4005: not in interfaces brief",
                "device": "lbnl-data-sw",
                "confirmed": False,
            }
        ],
    )
    rows = build_device_health_rows(topo)
    by_dev = {r["device"]: r for r in rows}
    assert by_dev["lbnl-data-sw"]["interfaces"] == "Inventory Review"
    assert "Mapping unknown" in by_dev["lbnl-data-sw"]["notes"]
