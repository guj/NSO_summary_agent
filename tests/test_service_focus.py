"""Tests for service-first focus (--service-type / --service-id)."""

from __future__ import annotations

from diagnostic_mas.case import Budget, CaseFile, add_diagnosis, add_evidence
from diagnostic_mas.dataplane_verify import (
    init_service_coverage,
    select_dataplane_candidates,
    service_needs_investigation,
)
from diagnostic_mas.focus import (
    endpoint_devices_from_services,
    resolve_spine_flags,
)
from diagnostic_mas.operator_report import format_services_operator


def test_service_focus_implies_service_only_flags():
    assert resolve_spine_flags(service_focus=True) == (False, False, True, False)
    # Layer-only still wins
    assert resolve_spine_flags(isis_only=True, service_focus=True) == (
        True,
        False,
        False,
        False,
    )


def test_endpoint_devices_from_services():
    services = {
        "l2ptp/a": {
            "name": "a",
            "devices": ["lbnl-data-sw"],
            "live_l2": {
                "endpoints": [
                    {"device": "lbnl-data-sw"},
                    {"device": "renc-data-sw"},
                ]
            },
        }
    }
    assert endpoint_devices_from_services(services) == [
        "lbnl-data-sw",
        "renc-data-sw",
    ]


def test_service_needs_investigation_and_coverage():
    healthy = {
        "name": "ok-svc",
        "status": "up",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "live_l2": {
            "summary": "up",
            "endpoints": [
                {"device": "a", "ac": "Hu0/0/0/1.1", "st": "UP"},
                {"device": "b", "ac": "Hu0/0/0/2.1", "st": "UP"},
            ],
        },
    }
    broken = {
        "name": "bad-svc",
        "status": "up",
        "system_status": "up",
        "dataplane_status": "not_checked",
        "live_l2": {
            "summary": "unknown",
            "endpoints": [
                {"device": "a", "ac": "x.1", "error": "ac_not_found"},
                {"device": "b", "ac": "y.1", "error": "ac_not_found"},
            ],
        },
    }
    assert service_needs_investigation(healthy) is False
    assert service_needs_investigation(broken) is True

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_drill_issues=1))
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "extra": {
                    "services": {
                        "l2ptp/ok-svc": {**healthy, "service_type": "l2ptp"},
                        "l2ptp/bad-svc": {**broken, "service_type": "l2ptp"},
                        "l2ptp/other": {
                            "name": "other",
                            "service_type": "l2ptp",
                            "status": "degraded",
                            "system_status": "up",
                            "dataplane_status": "not_checked",
                            "live_l2": {
                                "endpoints": [
                                    {"device": "a", "ac": "z", "st": "DN"},
                                ]
                            },
                        },
                    }
                }
            },
        },
    )
    init_service_coverage(case)
    assert case.service_coverage["ok-svc"] == "basic_passed"
    assert case.service_coverage["bad-svc"] == "needs_investigation"
    assert case.service_coverage["other"] == "needs_investigation"

    picked = select_dataplane_candidates(case, limit=1, suspicious_only=True)
    names = {r.get("name") for _e, r in picked}
    # Soft-error is preferred within the limit, but must not exceed it.
    assert names == {"bad-svc"}
    assert len(picked) == 1


def test_operator_report_shows_coverage():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    case.service_coverage = {
        "fabric-l2ptp-t1": "basic_passed",
        "other": "budget_skipped",
    }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "extra": {
                    "services": {
                        "l2ptp/fabric-l2ptp-t1": {
                            "name": "fabric-l2ptp-t1",
                            "service_type": "l2ptp",
                            "status": "up",
                            "system_status": "up",
                            "devices": ["a", "b"],
                        },
                        "l2ptp/other": {
                            "name": "other",
                            "service_type": "l2ptp",
                            "status": "degraded",
                            "devices": ["a"],
                        },
                    }
                }
            },
        },
    )
    text = "\n".join(format_services_operator(case, services_detail=True))
    assert "Basic checks passed" in text
    assert "Not investigated (budget limit)" in text
    assert "fabric-l2ptp-t1" in text
