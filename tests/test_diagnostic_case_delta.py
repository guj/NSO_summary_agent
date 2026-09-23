"""Tests for diagnostic case-to-case delta."""

from __future__ import annotations

import json
from pathlib import Path

from diagnostic_mas.case import Budget, CaseFile, open_issue
from diagnostic_mas.case_delta import (
    compare_run_dirs,
    compute_case_delta,
    format_case_delta,
    load_case_dict,
)
from diagnostic_mas.state_paths import case_to_dict, persist_case


def test_case_to_dict_includes_coverage_and_focus():
    case = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    case.focus_devices = ["renc-data-sw"]
    case.service_coverage = {"svc-a": "basic_passed", "svc-b": "unresolved"}
    d = case_to_dict(case)
    assert d["focus_devices"] == ["renc-data-sw"]
    assert d["service_coverage"]["svc-b"] == "unresolved"


def test_persist_case_writes_coverage(tmp_path: Path):
    case = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    case.service_coverage = {"fabric-l2ptp-t1": "investigated"}
    out = persist_case(
        tmp_path / "diagnostic_mas",
        run_id="2026-09-11T12:00:00Z",
        case=case,
        report="# r\n",
    )
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["service_coverage"]["fabric-l2ptp-t1"] == "investigated"


def test_new_recovered_persistent_and_coverage():
    older = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    open_issue(
        older,
        code="service_down",
        message="was down",
        evidence_ids=["ev_1"],
        layer="services",
        edge_id="svc-old",
    )
    open_issue(
        older,
        code="service_degraded",
        message="still bad",
        evidence_ids=["ev_2"],
        layer="services",
        edge_id="svc-persist",
    )
    older.service_coverage = {
        "svc-old": "unresolved",
        "svc-persist": "unresolved",
        "svc-skip": "investigated",
    }

    newer = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    open_issue(
        newer,
        code="service_degraded",
        message="still bad",
        evidence_ids=["ev_9"],
        layer="services",
        edge_id="svc-persist",
    )
    open_issue(
        newer,
        code="service_down",
        message="new failure",
        evidence_ids=["ev_8"],
        layer="services",
        edge_id="svc-new",
    )
    newer.service_coverage = {
        "svc-old": "basic_passed",
        "svc-persist": "unresolved",
        "svc-new": "unresolved",
        # svc-skip missing → not checked
    }

    delta = compute_case_delta(case_to_dict(older), case_to_dict(newer))
    new_ids = {i["edge_id"] for i in delta["new_problems"]}
    recovered_ids = {i["edge_id"] for i in delta["recovered"]}
    persist_ids = {i["edge_id"] for i in delta["persistent"]}
    assert new_ids == {"svc-new"}
    assert recovered_ids == set()
    assert "svc-old" in {r["service"] for r in delta["last_known_service_faults"]}
    assert persist_ids == {"svc-persist"}
    cov = {c["service"]: c["to"] for c in delta["coverage_changes"]}
    assert cov["svc-skip"] == "not_checked"
    missing = {m["subject"] for m in delta["newly_missing_evidence"]}
    assert "svc-skip" in missing
    failed = {f["service"] for f in delta["newly_failed_services"]}
    assert "svc-new" in failed

    text = format_case_delta(delta)
    assert "Newly detected service faults" in text
    assert "Newly missing evidence" in text
    assert "New problems" in text
    assert "svc-new" in text
    assert "svc-old" in text
    assert "svc-persist" in text
    assert "svc-skip" in text


def test_recovered_devices_and_incomplete_dig_missing_evidence():
    from diagnostic_mas.case import add_diagnosis
    from diagnostic_mas.case_delta import format_changes_since_previous

    older = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    open_issue(
        older,
        code="device_live_unreachable",
        message="WASH IS-IS timeout",
        evidence_ids=["ev_1"],
        layer="underlay",
        devices=["wash-data-sw"],
    )
    add_diagnosis(
        older,
        kind="dataplane",
        source="llm",
        status="up",
        observed="ok",
        cause="ready",
        subject={"name": "svc-a", "service_type": "l2ptp"},
        extra={"complete": True},
    )

    newer = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    add_diagnosis(
        newer,
        kind="dataplane",
        source="llm",
        status="down",
        observed="XC DN",
        cause="remote down",
        subject={"name": "svc-a", "service_type": "l2ptp"},
        extra={"complete": True},
    )
    add_diagnosis(
        newer,
        kind="dataplane",
        source="llm",
        status="unknown",
        observed="incomplete",
        cause="budget",
        subject={"name": "svc-b", "service_type": "l2sts"},
        extra={"complete": False},
    )

    newer.live_verified_devices = ["wash-data-sw"]
    delta = compute_case_delta(case_to_dict(older), case_to_dict(newer))
    assert delta["recovered_devices"] == ["wash-data-sw"]
    failed = {f["service"]: f["status"] for f in delta["newly_failed_services"]}
    assert failed.get("svc-a") == "down"
    missing_kinds = {m["kind"] for m in delta["newly_missing_evidence"]}
    assert "incomplete_dig" in missing_kinds
    subjects = {m["subject"] for m in delta["newly_missing_evidence"]}
    assert "svc-b" in subjects

    section = "\n".join(
        format_changes_since_previous(delta, previous_run_id="run-old")
    )
    assert "## Changes since previous run" in section
    assert "wash-data-sw" in section
    assert "svc-a" in section
    assert "svc-b" in section
    assert "Compared to `run-old`" in section


def test_report_includes_changes_section_before_devices():
    from diagnostic_mas.case_delta import compute_case_delta
    from diagnostic_mas.report import render_report
    from diagnostic_mas.state_paths import case_to_dict

    older = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    open_issue(
        older,
        code="device_live_unreachable",
        message="timeout",
        evidence_ids=["ev_1"],
        layer="underlay",
        devices=["scm-data-sw"],
    )
    newer = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    newer.live_verified_devices = ["scm-data-sw"]
    delta = compute_case_delta(case_to_dict(older), case_to_dict(newer))
    text = render_report(
        newer,
        case_delta=delta,
        previous_run_id="2026-09-17T12:00:00Z",
    )
    assert "## Changes since previous run" in text
    assert "scm-data-sw" in text
    assert text.index("## Changes since previous run") < text.index("## Devices")
    baseline = render_report(newer, include_changes_section=True, case_delta=None)
    assert "no previous snapshot" in baseline.lower()


def test_out_of_scope_not_counted_as_recovered():
    older = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    open_issue(
        older,
        code="service_down",
        message="only in older scope",
        evidence_ids=["ev_1"],
        layer="services",
        edge_id="svc-narrow",
    )
    older.service_coverage = {
        "svc-narrow": "unresolved",
        "svc-keep": "basic_passed",
    }

    newer = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    newer.service_coverage = {"svc-keep": "basic_passed"}

    delta = compute_case_delta(case_to_dict(older), case_to_dict(newer))
    assert delta["recovered"] == []
    assert {i["edge_id"] for i in delta["out_of_scope"]} == {"svc-narrow"}


def test_compare_run_dirs(tmp_path: Path):
    older = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    open_issue(
        older,
        code="isis_adj_down",
        message="adj down",
        evidence_ids=["ev_1"],
        layer="underlay",
        edge_id="a|b",
        devices=["a", "b"],
    )
    older.service_coverage = {}
    newer = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    newer.service_coverage = {}

    d1 = persist_case(
        tmp_path / "diagnostic_mas",
        run_id="run-old",
        case=older,
        report="# old\n",
    ).parent
    d2 = persist_case(
        tmp_path / "diagnostic_mas",
        run_id="run-new",
        case=newer,
        report="# new\n",
    ).parent

    text = compare_run_dirs(d1, d2)
    assert "Recovered problems" in text
    assert "a|b" in text
    loaded = load_case_dict(d1)
    assert "issues" in loaded


def _dp_case(status=None, complete=True):
    from diagnostic_mas.case import add_diagnosis
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    if status:
        add_diagnosis(case, kind="dataplane", source="llm", status=status,
                      observed="targeted observation", cause="label inconsistency",
                      subject={"name": "circuit", "service_type": "l2ptp"},
                      extra={"complete": complete})
    return case


def test_status_change_is_detection_not_proven_regression():
    from diagnostic_mas.case_delta import compact_delta_for_llm, format_changes_since_previous
    for previous_status in (None, "unknown", "up"):
        delta = compute_case_delta(case_to_dict(_dp_case(previous_status)),
                                   case_to_dict(_dp_case("down")))
        finding = delta["newly_failed_services"][0]
        assert finding["classification"] == "newly_detected_fault"
        assert finding["regression_proven"] is False
        assert "does not prove a regression" in "\n".join(format_changes_since_previous(delta))
        assert "comparable checks" in compact_delta_for_llm(delta)["regression_policy"]


def test_fault_survives_multiple_published_sampling_gaps(tmp_path):
    from diagnostic_mas.case_delta import service_fault_history, load_previous_case
    old = _dp_case("down")
    persist_case(tmp_path, run_id="r1", case=old)
    prior, previous_id = load_previous_case(tmp_path)
    for run in ("r2", "r3"):
        current = _dp_case()
        current.service_coverage = {"circuit": "basic_passed"}
        current.last_known_service_faults = service_fault_history(
            prior, case_to_dict(current), previous_run_id=previous_id, run_id=run)
        row = current.last_known_service_faults[0]
        assert row["status"] == "down"
        assert row["last_observed_run_id"] == "r1"
        assert not row["rechecked"]
        assert "not rechecked" in row["verification"]
        assert row["cause"] == "label inconsistency"
        assert not current.diagnoses
        persist_case(tmp_path, run_id=run, case=current)
        prior, previous_id = load_previous_case(tmp_path)
    inconclusive = case_to_dict(_dp_case("unknown", complete=False))
    history = service_fault_history(prior, inconclusive, run_id="r4")
    assert history[0]["last_observed_run_id"] == "r1"
    assert "inconclusive" in history[0]["verification"]
    assert service_fault_history(
        {**inconclusive, "last_known_service_faults": history},
        case_to_dict(_dp_case("up")), run_id="r5") == []
    # A nominal up with an incomplete investigation must not clear history.
    assert service_fault_history(prior, case_to_dict(_dp_case("up", False)))


def test_historical_fault_does_not_become_new_or_recovered_when_unsampled():
    from diagnostic_mas.case_delta import service_fault_history, format_changes_since_previous
    older = case_to_dict(_dp_case("down"))
    current = case_to_dict(_dp_case())
    current["service_coverage"] = {"other": "investigated"}
    delta = compute_case_delta(older, current)
    assert delta["newly_failed_services"] == []
    assert delta["last_known_service_faults"][0]["status"] == "down"
    assert "Last-known service faults" in "\n".join(format_changes_since_previous(delta))
    current["last_known_service_faults"] = service_fault_history(older, current, previous_run_id="r1")
    again = compute_case_delta(current, case_to_dict(_dp_case("down")))
    assert again["newly_failed_services"] == []


def test_inconclusive_recheck_does_not_make_old_fault_new_again():
    from diagnostic_mas.case_delta import service_fault_history
    old = case_to_dict(_dp_case("down"))
    gap = case_to_dict(_dp_case("unknown", False))
    gap["last_known_service_faults"] = service_fault_history(old, gap, previous_run_id="r1")
    delta = compute_case_delta(gap, case_to_dict(_dp_case("down")))
    assert delta["newly_failed_services"] == []


def test_unchecked_device_is_not_recovered_from_inventory_or_scope():
    issue = {"code": "device_live_unreachable", "devices": ["scm-data-sw"],
             "layer": "underlay", "status": "open"}
    old = {"issues": [issue]}
    for current in (
        {},
        {"device_names": ["scm-data-sw"]},
        {"focus_devices": ["scm-data-sw"]},
        {"focus_devices": ["cern-data-sw", "ucsd-data-sw"],
         "live_verified_devices": ["cern-data-sw", "ucsd-data-sw"]},
    ):
        delta = compute_case_delta(old, current)
        assert delta["recovered_devices"] == []
        assert delta["recovered"] == []
        assert issue in delta["out_of_scope"]
    current = {"live_verified_devices": ["scm-data-sw"]}
    delta = compute_case_delta(old, current)
    assert delta["recovered_devices"] == ["scm-data-sw"]
    assert issue in delta["recovered"]
    current["issues"] = [issue]
    assert compute_case_delta(old, current)["recovered_devices"] == []
