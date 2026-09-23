"""Tests for diagnostic_mas Phase-1 metrics snapshot."""

from __future__ import annotations

from diagnostic_mas.case import Budget, CaseFile, open_issue
from diagnostic_mas.ingest import ingest_layer_spine
from diagnostic_mas.metrics import metrics_snapshot_from_case
from nso_facts.metrics import build_phase1_metrics


def test_metrics_snapshot_includes_issues_from_ingest():
    """``ingest_layer_spine`` opens case.issues — metrics must count those."""
    case = CaseFile(
        budget=Budget(max_deep_checks=0, max_handoffs=0),
        device_names=["lbnl-data-sw", "renc-data-sw"],
    )
    ingest_layer_spine(
        case,
        layer="underlay",
        role="isis",
        static_summary={},
        operational_summary={"total": 2, "up": 2, "down": 0},
        issues=[
            {
                "code": "unknown_neighbor_system_id",
                "message": "unmapped star-data-sw",
                "layer": "underlay",
            }
        ],
        static_edges=[],
        operational_edges=[],
    )
    ingest_layer_spine(
        case,
        layer="routing",
        role="bgp",
        static_summary={},
        operational_summary={"total": 1, "up": 0, "down": 1},
        issues=[
            {
                "code": "unknown_neighbor_address",
                "message": "10.148.0.1",
                "layer": "routing",
            }
        ],
        static_edges=[],
        operational_edges=[],
    )
    ingest_layer_spine(
        case,
        layer="services",
        role="service",
        static_summary={},
        operational_summary={
            "l2ptp": {"up": 1, "down": 0, "degraded": 0, "unknown": 0},
        },
        issues=[],
        extra={
            "services": {
                "l2ptp/fabric-l2ptp-t1": {
                    "service_type": "l2ptp",
                    "name": "fabric-l2ptp-t1",
                    "status": "up",
                    "system_status": "up",
                    "dataplane_status": "up",
                }
            },
            "counts": {
                "l2ptp": {"up": 1, "down": 0, "degraded": 0, "unknown": 0},
            },
            "fleet_sync": {
                "status": "success",
                "data": {
                    "summary": {"in_sync": 2, "out_of_sync": 0, "error": 0},
                    "devices": [
                        {"device": "lbnl-data-sw", "result": "in-sync"},
                        {"device": "renc-data-sw", "result": "in-sync"},
                    ],
                },
            },
            "system_health": {},
            "hardware_health": {},
        },
    )
    # Later pipeline issues (e.g. dataplane) also land on case.issues
    open_issue(
        case,
        code="service_down",
        message="l2sts svc1 down",
        evidence_ids=[],
        layer="services",
        edge_id="svc1",
    )

    # Spine payloads must not carry a parallel issues list (ingest never writes one)
    for ev in case.evidence:
        if ev.get("kind") == "spine":
            assert "issues" not in (ev.get("payload") or {})

    snap = metrics_snapshot_from_case(case)
    assert snap["fleet_sync"]["status"] == "success"
    assert snap["counts"]["l2ptp"]["up"] == 1
    issues = snap["topology"]["operational"]["issues"]
    layers = {i.get("layer") for i in issues}
    assert "underlay" in layers
    assert "routing" in layers
    assert "services" in layers
    assert len(issues) == 3

    lines = build_phase1_metrics(
        snap, success=True, duration_seconds=12.5, pipeline="diagnostic"
    )
    text = "\n".join(lines)
    assert 'pipeline="diagnostic"' in text
    assert "nso_isis_adjacencies_up" in text
    assert 'nso_services_up{pipeline="diagnostic",service_type="l2ptp"}' in text or (
        'service_type="l2ptp"' in text and 'pipeline="diagnostic"' in text
    )
    assert 'nso_topology_issues{layer="underlay"' in text or (
        'layer="underlay"' in text and "nso_topology_issues" in text
    )
    assert 'nso_topology_issues{layer="services"' in text or (
        'layer="services"' in text and "nso_topology_issues" in text
    )
