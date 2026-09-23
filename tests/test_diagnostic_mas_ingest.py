from diagnostic_mas.case import Budget, CaseFile
from diagnostic_mas.ingest import ingest_layer_spine


def test_ingest_opens_issue_per_collector_issue():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    ingest_layer_spine(
        case,
        layer="underlay",
        role="isis",
        static_summary={"edge_count": 1},
        operational_summary={"up": 0, "down": 1},
        issues=[
            {
                "severity": "high",
                "layer": "underlay",
                "code": "adjacency_down",
                "edge_id": "a|b",
                "message": "renc↔lbnl down",
            }
        ],
        operational_edges=[{"id": "a|b", "status": "down"}],
    )
    assert len(case.evidence) == 1
    assert case.evidence[0]["kind"] == "spine"
    assert len(case.issues) == 1
    assert case.issues[0]["status"] == "open"
    assert case.issues[0]["code"] == "adjacency_down"


def test_ingest_preserves_live_l2_on_service_issue():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    live = {
        "summary": "degraded",
        "endpoints": [
            {"device": "lbnl-data-sw", "ac": "Hu0/0/0/17.100", "st": "UP"},
            {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "DN"},
        ],
    }
    ingest_layer_spine(
        case,
        layer="services",
        role="service",
        static_summary={},
        operational_summary={"degraded": 1},
        issues=[
            {
                "severity": "medium",
                "layer": "services",
                "code": "service_degraded",
                "edge_id": "bad",
                "message": "l2ptp bad: degraded — renc Hu0/0/0/0.100 AC DN",
                "devices": ["lbnl-data-sw", "renc-data-sw"],
                "live_l2": live,
                "in_sync": None,
                "device_sync": {"renc-data-sw": "in-sync"},
            }
        ],
    )
    issue = case.issues[0]
    assert issue["live_l2"]["summary"] == "degraded"
    assert issue["device_sync"]["renc-data-sw"] == "in-sync"
    assert "in_sync" in issue
    assert issue["in_sync"] is None
