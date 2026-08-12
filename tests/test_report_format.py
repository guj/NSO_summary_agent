"""Tests for deterministic report formatting."""

from agent.report_format import (
    assemble_report,
    assemble_report_plain,
    build_reports,
    delta_has_changes,
    format_delta_section,
    format_fleet_sync_summary,
    format_service_counts_plain,
    format_service_counts_table,
)


def test_service_counts_table_sorted_rows():
    counts = {
        "l2ptp": {"total": 2, "up": 2, "down": 0, "degraded": 0, "unknown": 0},
        "l3rt": {"total": 1, "up": 1, "down": 0, "degraded": 0, "unknown": 0},
    }
    table = format_service_counts_table(counts)
    assert "| Type | Total | Up | Down | Degraded | Unknown |" in table
    assert "| l2ptp | 2 | 2 | 0 | 0 | 0 |" in table
    assert "| l3rt | 1 | 1 | 0 | 0 | 0 |" in table
    assert table.index("l2ptp") < table.index("l3rt")


def test_delta_section_no_changes():
    delta = {
        "first_run": False,
        "counts": {},
        "new_failures": [],
        "recoveries": [],
        "status_changes": [],
        "removed": [],
    }
    assert format_delta_section(delta) == "No delta to report"
    assert delta_has_changes(delta) is False


def test_delta_section_first_run():
    delta = {"first_run": True, "counts": {}}
    assert format_delta_section(delta) == "First run — no previous snapshot to compare."


def test_delta_section_with_count_change():
    delta = {
        "first_run": False,
        "counts": {"l2ptp": {"total": 1, "up": 1, "down": 0, "degraded": 0, "unknown": 0}},
        "new_failures": [],
        "recoveries": [],
        "status_changes": [],
        "removed": [],
    }
    text = format_delta_section(delta)
    assert "l2ptp" in text
    assert "total +1" in text


def test_fleet_sync_all_in_sync():
    fleet = {
        "status": "success",
        "data": {
            "summary": {"in_sync": 3, "out_of_sync": 0, "error": 0},
            "devices": [
                {"device": "sw1", "result": "in-sync"},
                {"device": "sw2", "result": "in-sync"},
                {"device": "sw3", "result": "in-sync"},
            ],
        },
    }
    assert format_fleet_sync_summary(fleet) == "All 3 devices are in-sync"


def test_fleet_sync_lists_problems_when_not_all_in_sync():
    fleet = {
        "status": "success",
        "data": {
            "summary": {"in_sync": 2, "out_of_sync": 1, "error": 0},
            "devices": [
                {"device": "sw1", "result": "in-sync"},
                {"device": "sw2", "result": "out-of-sync"},
            ],
        },
    }
    text = format_fleet_sync_summary(fleet)
    assert "2 in-sync, 1 out-of-sync" in text
    assert "- sw2: out-of-sync" in text


def test_service_counts_plain_aligned():
    counts = {
        "l2ptp": {"total": 2, "up": 2, "down": 0, "degraded": 0, "unknown": 0},
        "l3rt": {"total": 1, "up": 1, "down": 0, "degraded": 0, "unknown": 0},
    }
    text = format_service_counts_plain(counts)
    assert "Type" in text
    assert "l2ptp" in text
    assert "l3rt" in text
    assert "|" not in text


def test_assemble_report_plain_structure():
    report = assemble_report_plain(
        run_id="2026-06-26T22:00:00Z",
        problems="None reported.",
        counts_block=format_service_counts_plain({}),
        delta_section="No delta to report",
        fleet_sync_section="All 3 devices are in-sync",
        ignored_types=["idipa"],
    )
    assert "NSO Ops Snapshot — 2026-06-26T22:00:00Z" in report
    assert "Problems / Failures" in report
    assert "Service Counts (by type)" in report
    assert "Ignored service types: idipa" in report
    assert "**" not in report


def test_build_reports_all_formats():
    outputs = build_reports(
        run_id="run-1",
        problems="svc-a is down.",
        counts={"l3rt": {"total": 1, "up": 0, "down": 1, "degraded": 0, "unknown": 0}},
        delta_section="No delta to report",
        fleet_sync_section="All 1 devices are in-sync",
        sections=("problems", "counts", "delta", "fleet_sync", "ignored_types"),
        topology=None,
    )
    assert "| l3rt |" in outputs.markdown
    assert "l3rt" in outputs.plain and "|" not in outputs.plain
    assert "<table" in outputs.html and "l3rt" in outputs.html


def test_html_renders_device_name_bold():
    topology = {
        "operational": {
            "layers": {
                "physical": {
                    "edges": [
                        {
                            "id": "if:sw1:Lo0",
                            "local": {"device": "sw1", "interface": "Loopback0"},
                            "state": {"admin": "up", "oper": "up", "status": "up"},
                        }
                    ]
                },
                "underlay": {"edges": []},
                "routing": {"edges": []},
            }
        }
    }
    out = build_reports(
        run_id="run-1",
        problems="None reported.",
        counts={},
        delta_section="No delta",
        fleet_sync_section="ok",
        sections=("devices",),
        topology=topology,
    )
    assert "Device: sw1" in out.markdown
    assert "Device: sw1" in out.html
    assert "Device: sw1" in out.plain
    assert "Detailed Device Analysis" in out.plain
    after = out.plain.split("Detailed Device Analysis", 1)[1]
    assert "Device: sw1" in after


def test_assemble_report_structure():
    report = assemble_report(
        run_id="2026-06-26T22:00:00Z",
        problems="None reported.",
        counts_table=format_service_counts_table({}),
        delta_section="No delta to report",
        fleet_sync_section="All 3 devices are in-sync",
        ignored_types=["idipa"],
    )
    assert "**NSO Ops Snapshot — 2026-06-26T22:00:00Z**" in report
    assert "**Service Counts (by type)**" in report
    assert "**Delta since last run**\nNo delta to report" in report
    assert "**Ignored service types**\nidipa" in report


def test_build_reports_respects_section_order_and_omission():
    out = build_reports(
        run_id="t1",
        problems="Boom",
        counts={"l2ptp": {"total": 1, "up": 1, "down": 0, "degraded": 0, "unknown": 0}},
        delta_section="No delta to report",
        fleet_sync_section="All 1 devices are in-sync",
        ignored_types=["idipa"],
        sections=("counts", "devices", "problems"),
        topology=None,
    )
    md = out.markdown
    assert md.index("Service Counts") < md.index("Detailed Device Analysis")
    assert md.index("Detailed Device Analysis") < md.index("Problems / Failures")
    assert "Delta since last run" not in md
    assert "Fleet sync" not in md
    assert "No device topology in snapshot." in md


def test_build_reports_default_includes_devices_heading_when_sections_default():
    from agent.config import DEFAULT_REPORT_SECTIONS

    out = build_reports(
        run_id="t1",
        problems="None reported.",
        counts={},
        delta_section="No delta to report",
        fleet_sync_section="Fleet sync: unavailable",
        system_health="All devices reporting low CPU.",
        ignored_types=[],
        sections=DEFAULT_REPORT_SECTIONS,
        topology=None,
        executive_narrative=(
            "Overall Status\n--------------\n🟢 Services: ok\n\n"
            "Action Items\n------------\nNone reported."
        ),
        delta={"first_run": True, "counts": {}},
    )
    assert "NSO Operations Snapshot" in out.markdown
    assert (
        "=====================================================\n"
        "Detailed Device Analysis\n"
        "====================================================="
    ) in out.markdown
    assert "**Infrastructure Health**" not in out.markdown
    assert "Problems / Failures" not in out.markdown
    assert out.markdown.index("Overall Status") < out.markdown.index(
        "Detailed Device Analysis"
    )
    assert "Fleet Summary" in out.markdown
    assert out.markdown.index("Overall Status") < out.markdown.index("Fleet Summary")
    assert out.markdown.index("Fleet Summary") < out.markdown.index("Action Items")
    assert "**Fleet sync**" not in out.markdown
    assert "OK Services" in out.plain or "Overall Status" in out.plain
