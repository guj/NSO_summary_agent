from diagnostic_mas.roles.service import (
    format_live_l2_cause,
    issues_from_service_health,
)


def test_issues_from_service_health_flat_map():
    services = {
        "l2ptp/good": {
            "service_type": "l2ptp",
            "name": "good",
            "status": "up",
            "devices": ["a", "b"],
        },
        "l2ptp/bad": {
            "service_type": "l2ptp",
            "name": "bad",
            "status": "degraded",
            "devices": ["renc-data-sw", "lbnl-data-sw"],
        },
    }
    issues = issues_from_service_health(services)
    assert len(issues) == 1
    assert issues[0]["code"] == "service_degraded"
    assert issues[0]["edge_id"] == "bad"


def test_issues_from_service_health_nested_instances():
    services = {
        "l2ptp": {
            "instances": [
                {"name": "x", "status": "down", "devices": ["a"]},
                {"name": "y", "status": "up", "devices": ["b"]},
            ]
        }
    }
    issues = issues_from_service_health(services)
    assert len(issues) == 1
    assert issues[0]["code"] == "service_down"


def test_format_live_l2_cause_lists_ac_states():
    live = {
        "summary": "degraded",
        "endpoints": [
            {
                "device": "lbnl-data-sw",
                "ac": "Hu0/0/0/17.100",
                "st": "UP",
            },
            {
                "device": "renc-data-sw",
                "ac": "Hu0/0/0/0.100",
                "st": "DN",
            },
        ],
    }
    text = format_live_l2_cause(live)
    assert "renc-data-sw" in text
    assert "Hu0/0/0/0.100" in text
    assert "XC DN" in text
    assert "lbnl-data-sw" in text
    assert "XC UP" in text


def test_issues_include_live_l2_cause_in_message():
    services = {
        "l2ptp/bad": {
            "service_type": "l2ptp",
            "name": "bad",
            "status": "degraded",
            "devices": ["renc-data-sw", "lbnl-data-sw"],
            "live_l2": {
                "summary": "degraded",
                "endpoints": [
                    {"device": "lbnl-data-sw", "ac": "Hu0/0/0/17.100", "st": "UP"},
                    {"device": "renc-data-sw", "ac": "Hu0/0/0/0.100", "st": "DN"},
                ],
            },
        },
    }
    issues = issues_from_service_health(services)
    assert len(issues) == 1
    msg = issues[0]["message"]
    assert "degraded" in msg
    assert "Hu0/0/0/0.100" in msg
    assert "DN" in msg
    assert issues[0]["live_l2"]["summary"] == "degraded"
