"""Tests for diagnostic_mas Phase-1 metrics snapshot."""

from __future__ import annotations

from diagnostic_mas.case import Budget, CaseFile, add_evidence, open_issue
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


def _l2sts(name: str, far_end: str, *, down: bool) -> dict:
    """An l2sts service between ``far_end`` and fiu; when down, the failed check is at fiu."""
    devices = [far_end, "fiu-data-sw"]
    record = {"name": name, "service_type": "l2sts", "devices": devices,
              "device_sync": {d: "in-sync" for d in devices}}
    if not down:
        return {**record, "operational_status": "up"}
    return {**record, "basic_checks": {"status": "down", "checks": [
        {"check": "attachment", "device": d, "observation": "Hu0/0/0/1.0",
         "status": "fault" if d == "fiu-data-sw" else "pass"} for d in devices]}}


def scan_case() -> CaseFile:
    """One l2sts up, two down at fiu, one l3rt with no operational evidence; two devices not covered."""
    case = CaseFile(
        budget=Budget(max_deep_checks=0, max_handoffs=0),
        device_names=["cien-data-sw", "fiu-data-sw", "mass-data-sw", "rutg-data-sw", "scm-data-sw"],
    )
    services = {
        "l2sts/ok": _l2sts("ok", "rutg-data-sw", down=False),
        "l2sts/d1": _l2sts("d1", "rutg-data-sw", down=True),
        "l2sts/d2": _l2sts("d2", "mass-data-sw", down=True),
        "l3rt/u1": {"name": "u1", "service_type": "l3rt", "devices": ["cien-data-sw"],
                    "device_sync": {"cien-data-sw": "in-sync"}},
    }
    eid = add_evidence(
        case,
        {"kind": "spine", "role": "service", "layer": "services",
         "payload": {"extra": {"services": services}}},
    )
    open_issue(case, code="device_live_unreachable", layer="device", evidence_ids=[eid],
               message="scm-data-sw: live query timed out", devices=["scm-data-sw"])
    open_issue(case, code="collection_error", layer="routing", evidence_ids=[eid],
               message="cien-data-sw: BGP summary collection failed: exec action unavailable")
    return case


def _pushed(case: CaseFile, **snapshot_fields) -> str:
    snapshot = metrics_snapshot_from_case(case, **snapshot_fields)
    return "\n".join(build_phase1_metrics(
        snapshot, success=True, duration_seconds=1.0, pipeline="diagnostic"))


def test_scan_pushes_the_reports_final_status_with_each_status_separate():
    text = _pushed(scan_case())

    for status, count in (("up", 1), ("down", 2), ("degraded", 0), ("unknown", 0)):
        assert (f'nso_services{{pipeline="diagnostic",service_type="l2sts",status="{status}"}} '
                f"{float(count)}") in text
    assert 'nso_services{pipeline="diagnostic",service_type="l3rt",status="unknown"} 1.0' in text
    assert "nso_services_down" not in text and "nso_services_up" not in text


def test_scan_pushes_faulted_services_by_the_device_they_point_to():
    text = _pushed(scan_case())

    assert ('nso_service_faults{device="fiu-data-sw",pipeline="diagnostic",'
            'service_type="l2sts",status="down"} 2.0') in text
    assert ('nso_service_faults{device="cien-data-sw",pipeline="diagnostic",'
            'service_type="l3rt",status="unknown"} 1.0') in text
    assert 'nso_service_faults{device="rutg-data-sw"' not in text


def test_scan_pushes_how_many_devices_it_could_not_cover():
    assert 'nso_fleet_devices_not_covered{pipeline="diagnostic"} 2.0' in _pushed(scan_case())


def test_successful_scan_pushes_a_good_last_attempt():
    text = _pushed(scan_case())

    assert 'nso_scan_last_attempt_success{pipeline="diagnostic"} 1.0' in text
    assert 'nso_scan_last_attempt_nso_unreachable{pipeline="diagnostic"} 0.0' in text


def test_scan_pushes_its_run_id_and_report_link():
    text = _pushed(scan_case(), run_id="run-1",
                   report_url="https://reports.example/run-1/report.html")

    assert ('nso_scan_info{pipeline="diagnostic",'
            'report_url="https://reports.example/run-1/report.html",run_id="run-1"} 1.0') in text
