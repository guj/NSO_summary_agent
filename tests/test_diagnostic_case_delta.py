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


def test_italic_lines_of_the_changes_show_as_italics_in_the_html_report_not_as_underscores():
    from agent.markdown_channels import markdown_to_html
    from diagnostic_mas.case_delta import format_case_delta, format_changes_since_previous

    issue = {"code": "collection_error", "layer": "underlay", "status": "open", "message": "no answer"}
    delta = {"notes": ["Older scan had no service step."], "persistent": [issue, dict(issue, message="again")]}
    section = format_changes_since_previous(delta, previous_run_id="run-old")
    assert any("Unchanged open items" in line for line in section)

    for text in ("\n".join(section), format_case_delta(delta, older_label="run-old", newer_label="run-new")):
        page = markdown_to_html(text)
        assert "<em>Note: Older scan had no service step.</em>" in page
        assert "_Note" not in page and "._" not in page and "_Unchanged" not in page
    page = markdown_to_html("\n".join(section))
    assert "<em>Compared to <code>run-old</code>.</em>" in page
    assert "<em>Unchanged open items" in page and "not re-summarized as new findings.</em>" in page


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


FULL_SCOPE = {"Device filter": "All", "Service type filter": "All",
              "Service ID filter": "All", "Scope flags": "Default"}


def _scan(services: dict[str, str], *, coverage: dict[str, str] | None = None,
          scope: dict | None = FULL_SCOPE, focus: list[str] | None = None,
          listed: list[str] | None = None) -> dict:
    """A saved case holding a service inventory (name -> service type).

    ``listed`` is the set of types the scan asked NSO for and got a whole
    answer to, which can include a type with no instances.
    """
    records = {f"{kind}/{name}": {"name": name, "service_type": kind, "devices": ["pe1"]}
               for name, kind in services.items()}
    extra: dict = {"services": records}
    if listed is not None:
        extra["service_types_listed"] = list(listed)
    case = {
        "evidence": [{"kind": "spine", "role": "service", "layer": "services",
                      "payload": {"extra": extra}}],
        "issues": [], "diagnoses": [],
        "service_coverage": coverage if coverage is not None else {n: "basic_passed" for n in services},
        "focus_devices": focus or [],
    }
    if scope is not None:
        case["run_configuration"] = dict(scope)
    return case


def test_service_gone_from_a_full_scan_is_reported_no_longer_present_not_as_missing_evidence():
    older = _scan({"keep": "l2bridge", "gone": "l2bridge"},
                  coverage={"keep": "basic_passed", "gone": "category_peer_skipped"})
    newer = _scan({"keep": "l2bridge"})

    delta = compute_case_delta(older, newer)

    assert delta["services_no_longer_present"] == [
        {"service": "gone", "service_type": "l2bridge",
         "previous_coverage": "category_peer_skipped"}
    ]
    assert delta["newly_missing_evidence"] == []


def test_service_outside_a_narrower_scan_is_still_missing_evidence_not_reported_gone():
    older = _scan({"keep": "l2bridge", "other": "l2bridge"},
                  coverage={"keep": "basic_passed", "other": "category_peer_skipped"})
    narrower = _scan({"keep": "l2bridge"}, scope={**FULL_SCOPE, "Service ID filter": "keep"})

    delta = compute_case_delta(older, narrower)

    assert delta["services_no_longer_present"] == []
    assert [row["subject"] for row in delta["newly_missing_evidence"]] == ["other"]


def test_service_is_not_reported_gone_when_its_type_was_not_collected_or_scope_is_unknown():
    older = _scan({"keep": "l2bridge", "other": "l3rt"},
                  coverage={"keep": "basic_passed", "other": "category_peer_skipped"})

    type_not_collected = compute_case_delta(older, _scan({"keep": "l2bridge"}))
    scope_unknown = compute_case_delta(
        _scan({"keep": "l2bridge", "other": "l2bridge"},
              coverage={"keep": "basic_passed", "other": "category_peer_skipped"}),
        _scan({"keep": "l2bridge"}, scope=None),
    )

    assert type_not_collected["services_no_longer_present"] == []
    assert scope_unknown["services_no_longer_present"] == []


def test_services_of_a_type_the_scan_listed_as_empty_are_no_longer_present():
    older = _scan({"keep": "l2bridge", "m1": "port-mirror", "m2": "port-mirror"})
    newer = _scan({"keep": "l2bridge"}, listed=["l2bridge", "port-mirror"])

    delta = compute_case_delta(older, newer)

    assert [row["service"] for row in delta["services_no_longer_present"]] == ["m1", "m2"]
    assert delta["newly_missing_evidence"] == []


def _faulty_then_gone(**newer_scan) -> tuple[dict, dict]:
    """Older run: `faulty` is a last-known Down. Newer run: only `keep` is listed."""
    older = _scan({"keep": "l3rt", "faulty": "l3rt"})
    older["issues"] = [{"layer": "services", "code": "service_down", "status": "open",
                        "edge_id": "faulty", "message": "duplicate gateway"}]
    older["last_known_service_faults"] = [{
        "service": "faulty", "status": "down", "service_type": "l3rt",
        "last_observed_run_id": "r1", "cause": "duplicate gateway"}]
    return older, _scan({"keep": "l3rt"}, **newer_scan)


def test_last_known_fault_ends_when_a_full_scan_no_longer_lists_the_service():
    from diagnostic_mas.case_delta import service_fault_history

    older, newer = _faulty_then_gone()

    assert service_fault_history(older, newer) == []
    delta = compute_case_delta(older, newer)
    assert delta["last_known_service_faults"] == []
    assert [row["service"] for row in delta["services_no_longer_present"]] == ["faulty"]
    # Gone is not fixed: the old fault must not be counted as a recovery either.
    assert delta["recovered"] == [] and delta["out_of_scope"] == []
    assert delta["newly_missing_evidence"] == []


def test_fault_ends_for_a_service_removed_while_the_scan_ran():
    from diagnostic_mas.case_delta import service_fault_history

    older, newer = _faulty_then_gone()
    records = newer["evidence"][0]["payload"]["extra"]["services"]
    records["l3rt/faulty"] = {"name": "faulty", "service_type": "l3rt", "devices": ["pe1"],
                              "presence_recheck": {"outcome": "absent"}}

    assert service_fault_history(older, newer) == []


def test_service_removed_during_the_previous_scan_is_not_reported_again():
    older = _scan({"keep": "l2bridge", "left": "l2bridge"},
                  coverage={"keep": "basic_passed", "left": "needs_investigation"})
    records = older["evidence"][0]["payload"]["extra"]["services"]
    records["l2bridge/left"]["presence_recheck"] = {"outcome": "absent"}

    delta = compute_case_delta(older, _scan({"keep": "l2bridge"}))

    assert delta["newly_missing_evidence"] == []
    assert delta["services_no_longer_present"] == []


def test_last_known_fault_is_kept_when_a_narrower_scan_leaves_the_service_out():
    from diagnostic_mas.case_delta import service_fault_history

    older, narrower = _faulty_then_gone(scope={**FULL_SCOPE, "Service ID filter": "keep"})

    history = service_fault_history(older, narrower)

    assert [row["service"] for row in history] == ["faulty"]
    assert "not rechecked" in history[0]["verification"]


def test_changes_section_lists_services_no_longer_present_apart_from_missing_evidence():
    from diagnostic_mas.case_delta import compact_delta_for_llm, format_changes_since_previous

    older = _scan({"keep": "l2bridge", "gone": "l2bridge"},
                  coverage={"keep": "basic_passed", "gone": "category_peer_skipped"})
    delta = compute_case_delta(older, _scan({"keep": "l2bridge"}))

    text = "\n".join(format_changes_since_previous(delta, previous_run_id="run-1"))

    assert "**Services no longer present since the previous run:**" in text
    assert "intent is unknown" not in text and "owners" not in text
    assert "- `l2bridge/gone`" in text
    assert "not_checked" not in text and "coverage_gap" not in text
    for_summary = compact_delta_for_llm(delta)
    assert for_summary["services_no_longer_present"]["services"] == ["gone"]
    assert for_summary["newly_missing_evidence"] == []



BGP_EDGE = "bgp:10.0.0.1:10.0.1.1:hub-data-sw:pe1-data-sw"
ISIS_EDGE = "isis:hub-data-sw:HundredGigE0/0/0/1.3000:pe1-data-sw:HundredGigE0/0/0/2.3000"


def _routing_scan(bgp: dict | None = None, isis: dict | None = None, *,
                  verified: tuple[str, ...] = ("hub-data-sw", "pe1-data-sw"),
                  scope: dict | None = FULL_SCOPE, issues: tuple[dict, ...] = ()) -> dict:
    """A saved case holding routing sessions (edge id -> state). None: protocol not collected."""
    evidence = [
        {"kind": "spine", "role": role,
         "payload": {"operational_edges": [{"id": edge, "state": dict(state)} for edge, state in edges.items()]}}
        for role, edges in (("bgp", bgp), ("isis", isis)) if edges is not None
    ]
    case = {"evidence": evidence, "issues": list(issues), "diagnoses": [], "service_coverage": {},
            "focus_devices": [], "device_names": ["hub-data-sw", "pe1-data-sw"],
            "live_verified_devices": list(verified)}
    if scope is not None:
        case["run_configuration"] = dict(scope)
    return case


UP = {"local": "up", "remote": "up", "status": "up"}
BOTH = {BGP_EDGE: {"local": "established", "remote": "established", "status": "up"}}


def test_session_the_devices_now_report_down_is_listed_as_lost():
    newer = _routing_scan(bgp={BGP_EDGE: {"local": "idle", "remote": "idle", "status": "down"}})

    lost = compute_case_delta(_routing_scan(bgp=BOTH), newer)["routing_sessions_lost"]

    assert lost == [{"protocol": "bgp", "edge_id": BGP_EDGE, "now": "down", "states": "idle / idle"}]


def test_every_session_of_a_protocol_now_reported_down_is_listed():
    other = "bgp:10.0.0.1:10.0.2.1:hub-data-sw:pe1-data-sw"
    down = {"local": "idle", "remote": "idle", "status": "down"}

    lost = compute_case_delta(_routing_scan(bgp={BGP_EDGE: BOTH[BGP_EDGE], other: BOTH[BGP_EDGE]}),
                              _routing_scan(bgp={BGP_EDGE: down, other: down}))["routing_sessions_lost"]

    assert [row["edge_id"] for row in lost] == [BGP_EDGE, other]


def test_adjacency_neither_answering_device_lists_any_more_is_lost():
    lost = compute_case_delta(_routing_scan(isis={ISIS_EDGE: UP}), _routing_scan(isis={}))["routing_sessions_lost"]

    assert lost == [{"protocol": "isis", "edge_id": ISIS_EDGE, "now": "not reported", "states": ""}]


def test_missing_adjacency_is_not_called_lost_without_an_answer_from_both_ends():
    older = _routing_scan(isis={ISIS_EDGE: UP})
    failed = {"layer": "underlay", "code": "collection_error", "status": "open",
              "message": "pe1-data-sw: check_isis_adjacencies failed: timeout"}
    not_evidence = {
        "one end not queried": _routing_scan(isis={}, verified=("hub-data-sw",)),
        "one end failed this query": _routing_scan(isis={}, issues=(failed,)),
        "filtered scan": _routing_scan(isis={}, scope={**FULL_SCOPE, "Device filter": "hub-data-sw"}),
        "scope unknown": _routing_scan(isis={}, scope=None),
        "protocol not collected": _routing_scan(bgp={}),
    }

    for why, newer in not_evidence.items():
        assert compute_case_delta(older, newer)["routing_sessions_lost"] == [], why


def test_lost_adjacency_is_listed_once_when_parallel_links_were_cross_paired():
    """Two parallel links show up as extra ids pairing one link's end with the other's."""
    edge = lambda a, b: f"isis:hub-data-sw:HundredGigE0/0/0/1.{a}:pe1-data-sw:HundredGigE0/0/0/2.{b}"
    older = _routing_scan(isis={edge(10, 10): UP, edge(10, 20): UP, edge(20, 20): UP})
    newer = _routing_scan(isis={edge(10, 10): UP})

    lost = compute_case_delta(older, newer)["routing_sessions_lost"]

    # Only the link whose two ends both dropped out; the cross-paired id is not a second loss.
    assert [row["edge_id"] for row in lost] == [edge(20, 20)]


def test_session_still_up_or_of_unknown_state_is_not_lost():
    for state in ({"local": "established", "remote": "established", "status": "up"},
                  {"local": "established", "remote": "unknown", "status": "unknown"}):
        delta = compute_case_delta(_routing_scan(bgp=BOTH), _routing_scan(bgp={BGP_EDGE: state}))
        assert delta["routing_sessions_lost"] == []


# pe1-data-sw could not be read in the previous run, so only hub-data-sw's end was recorded, as up.
HUB_ONLY = {"local": "established", "remote": "unknown", "status": "unknown"}
ONE_SIDED = "isis:hub-data-sw:HundredGigE0/0/0/1.3000:pe1-data-sw:?"
HUB_UP = {"local": "up", "remote": "unknown", "status": "unknown"}


def test_session_up_at_the_end_that_was_read_is_lost_when_that_end_now_reports_it_down():
    newer = _routing_scan(bgp={BGP_EDGE: {"local": "idle", "remote": "unknown", "status": "down"}},
                          isis={ONE_SIDED: {"local": "init", "remote": "unknown", "status": "unknown"}},
                          verified=("hub-data-sw",))

    lost = compute_case_delta(_routing_scan(bgp={BGP_EDGE: HUB_ONLY}, isis={ONE_SIDED: HUB_UP}), newer)

    assert lost["routing_sessions_lost"] == [
        {"protocol": "bgp", "edge_id": BGP_EDGE, "now": "down", "states": "idle / unknown"},
        {"protocol": "isis", "edge_id": ONE_SIDED, "now": "down", "states": "init / unknown"}]


def test_adjacency_up_at_the_end_that_was_read_is_lost_when_that_end_answers_without_it():
    older = _routing_scan(isis={ONE_SIDED: HUB_UP})

    lost = compute_case_delta(older, _routing_scan(isis={}, verified=("hub-data-sw",)))["routing_sessions_lost"]

    assert lost == [{"protocol": "isis", "edge_id": ONE_SIDED, "now": "not reported", "states": "",
                     "answered": "hub-data-sw"}]
    # Both ends answered this time: the plain both-answered loss.
    [row] = compute_case_delta(older, _routing_scan(isis={}))["routing_sessions_lost"]
    assert "answered" not in row


def test_adjacency_up_at_one_end_is_not_lost_while_that_end_still_lists_it_or_did_not_answer():
    older = _routing_scan(isis={ONE_SIDED: HUB_UP})
    failed = {"layer": "underlay", "code": "collection_error", "status": "open",
              "message": "hub-data-sw: check_isis_adjacencies failed: timeout"}
    not_lost = {
        "still one-sided and up": _routing_scan(isis={ONE_SIDED: HUB_UP}, verified=("hub-data-sw",)),
        # pe1-data-sw answered this time, so hub's end is listed under the full id; pe1's view is
        # newly seen, not a loss of anything seen up before.
        "still listed under its full id": _routing_scan(
            isis={ISIS_EDGE: {"local": "up", "remote": "not listed", "status": "unidirectional"}}),
        "the end that saw it failed this query": _routing_scan(isis={}, issues=(failed,)),
        "the end that saw it was not queried": _routing_scan(isis={}, verified=("pe1-data-sw",)),
        "filtered scan": _routing_scan(isis={}, scope={**FULL_SCOPE, "Device filter": "hub-data-sw"}),
    }

    for why, newer in not_lost.items():
        assert compute_case_delta(older, newer)["routing_sessions_lost"] == [], why


def test_changes_section_names_the_end_that_no_longer_lists_an_adjacency():
    from diagnostic_mas.case_delta import format_changes_since_previous

    delta = compute_case_delta(_routing_scan(isis={ONE_SIDED: HUB_UP}),
                               _routing_scan(isis={}, verified=("hub-data-sw",)))

    text = "\n".join(format_changes_since_previous(delta, previous_run_id="run-1"))

    assert ("- IS-IS `hub-data-sw` HundredGigE0/0/0/1.3000 ↔ `pe1-data-sw` — not reported this run by"
            " `hub-data-sw`, which listed it up last run; `pe1-data-sw` was not read") in text


def test_changes_section_and_summary_input_list_lost_routing_sessions():
    from diagnostic_mas.case_delta import (
        compact_delta_for_llm, delta_has_operational_changes, format_changes_since_previous)

    delta = compute_case_delta(
        _routing_scan(bgp=BOTH, isis={ISIS_EDGE: UP}),
        _routing_scan(bgp={BGP_EDGE: {"local": "idle", "remote": "idle", "status": "down"}}, isis={}),
    )

    text = "\n".join(format_changes_since_previous(delta, previous_run_id="run-1"))

    assert delta_has_operational_changes(delta)
    assert "- BGP `hub-data-sw` 10.0.0.1 ↔ `pe1-data-sw` 10.0.1.1 — down (idle / idle)" in text
    assert ("- IS-IS `hub-data-sw` HundredGigE0/0/0/1.3000 ↔ `pe1-data-sw` HundredGigE0/0/0/2.3000"
            " — not reported this run; both devices answered") in text
    assert compact_delta_for_llm(delta)["routing_sessions_lost"]["count"] == 2


def _interface_scan(states: dict[str, tuple[str | None, str | None]]) -> dict:
    """A saved case holding interface states: "device:interface" -> (admin, oper)."""
    edges = [{"id": f"if:{key}", "state": {"admin": admin, "oper": oper}}
             for key, (admin, oper) in states.items()]
    return {"evidence": [{"kind": "spine", "role": "service",
                          "payload": {"extra": {"services": {}, "physical_operational_edges": edges}}}],
            "issues": [], "diagnoses": [], "service_coverage": {}, "focus_devices": []}


UPLINK, ACCESS = "pe1-data-sw:HundredGigE0/0/0/23", "pe1-data-sw:HundredGigE0/0/0/5"


def test_interface_that_went_down_is_listed_with_its_sub_interfaces_folded_in():
    older = _interface_scan({UPLINK: ("up", "up"), UPLINK + ".3000": ("up", "up"), ACCESS: ("up", "up")})
    newer = _interface_scan({UPLINK: ("down", "down"), UPLINK + ".3000": ("down", "down"), ACCESS: ("up", "up")})

    assert compute_case_delta(older, newer)["interfaces_lost"] == [
        {"device": "pe1-data-sw", "interface": "HundredGigE0/0/0/23", "now": "down", "sub_interfaces": 1,
         "services_deleted": 0, "services_down_before": 0, "services_on_port": 0}]


def test_sub_interface_down_under_a_working_port_and_a_shut_port_are_listed_as_such():
    older = _interface_scan({UPLINK: ("up", "up"), UPLINK + ".3000": ("up", "up"), ACCESS: ("up", "up")})
    newer = _interface_scan({UPLINK: ("up", "up"), UPLINK + ".3000": ("down", "down"),
                             ACCESS: ("admin-down", "admin-down")})

    assert compute_case_delta(older, newer)["interfaces_lost"] == [
        {"device": "pe1-data-sw", "interface": "HundredGigE0/0/0/23.3000", "now": "down", "sub_interfaces": 0,
         "services_deleted": 0, "services_down_before": 0, "services_on_port": 0},
        {"device": "pe1-data-sw", "interface": "HundredGigE0/0/0/5", "now": "admin-down", "sub_interfaces": 0,
         "services_deleted": 0, "services_down_before": 0, "services_on_port": 0}]


def test_interface_is_not_called_lost_without_a_down_state_this_run():
    older = _interface_scan({UPLINK: ("up", "up"), ACCESS: ("down", "down")})
    not_evidence = {
        "gone from this run's list": _interface_scan({}),
        "state not collected": _interface_scan({UPLINK: (None, None), ACCESS: ("down", "down")}),
        "still up; other was never up": _interface_scan({UPLINK: ("up", "up"), ACCESS: ("down", "down")}),
    }

    for why, newer in not_evidence.items():
        assert compute_case_delta(older, newer)["interfaces_lost"] == [], why


def test_changes_section_and_summary_input_list_interfaces_that_went_down():
    from diagnostic_mas.case_delta import (
        compact_delta_for_llm, delta_has_operational_changes, format_changes_since_previous)

    delta = compute_case_delta(
        _interface_scan({UPLINK: ("up", "up"), UPLINK + ".3000": ("up", "up"), ACCESS: ("up", "up")}),
        _interface_scan({UPLINK: ("down", "down"), UPLINK + ".3000": ("down", "down"),
                         ACCESS: ("admin-down", "admin-down")}),
    )

    text = "\n".join(format_changes_since_previous(delta, previous_run_id="run-1"))

    assert delta_has_operational_changes(delta)
    assert ("- `pe1-data-sw`: HundredGigE0/0/0/23 (+1 sub-interface), "
            "HundredGigE0/0/0/5 (admin-down)") in text
    assert compact_delta_for_llm(delta)["interfaces_lost"]["count"] == 2



LANE0, LANE1 = "HundredGigE0/0/0/64/0", "HundredGigE0/0/0/64/1"


def _ports_scan(on_ports: dict[str, str], states: dict[str, str]) -> dict:
    """A full scan: l2bridge services on pe1-data-sw (name -> port) and port oper states (port -> state)."""
    case = _scan({name: "l2bridge" for name in on_ports})
    extra = case["evidence"][0]["payload"]["extra"]
    for name, port in on_ports.items():
        kind, number = port.split("0/", 1)
        extra["services"][f"l2bridge/{name}"]["basic_checks"] = {"intent": {
            "device": "pe1-data-sw", "interface": [{"type": kind, "id": "0/" + number, "outervlan": 0}]}}
    extra["physical_operational_edges"] = [
        {"id": f"if:pe1-data-sw:{port}", "state": {"admin": state, "oper": state}} for port, state in states.items()]
    return case


def _lanes_after_deletions() -> dict:
    """Three ports went down: lane 0 lost its only service, lane 1 kept one of two, port 5 had none."""
    older = _ports_scan({"s1": LANE0, "s2": LANE1, "s3": LANE1},
                        {LANE0: "up", LANE1: "up", "HundredGigE0/0/0/5": "up"})
    newer = _ports_scan({"s3": LANE1}, {LANE0: "down", LANE1: "down", "HundredGigE0/0/0/5": "down"})
    return compute_case_delta(older, newer)


def test_interface_that_went_down_after_all_its_services_were_deleted_is_marked():
    marked = {row["interface"]: row["services_deleted"] for row in _lanes_after_deletions()["interfaces_lost"]}

    assert marked == {LANE0: 1, LANE1: 0, "HundredGigE0/0/0/5": 0}


def test_changes_section_and_summary_input_say_the_services_were_deleted():
    from diagnostic_mas.case_delta import compact_delta_for_llm, format_changes_since_previous

    delta = _lanes_after_deletions()
    delta["interfaces_lost"].append({"device": "pe2-data-sw", "interface": "HundredGigE0/0/0/7",
                                     "now": "down", "sub_interfaces": 0, "services_deleted": 2})
    text = "\n".join(format_changes_since_previous(delta, previous_run_id="run-1"))
    summary_input = compact_delta_for_llm(delta)["interfaces_lost"]

    assert f"- `pe1-data-sw`: HundredGigE0/0/0/5, {LANE0} (its service was deleted), {LANE1}" in text
    assert "- `pe2-data-sw`: HundredGigE0/0/0/7 (its 2 services were deleted)" in text
    assert f"pe1-data-sw: HundredGigE0/0/0/5, {LANE0} (its service was deleted), {LANE1}" in summary_input["by_device"]
    assert "deleted" in summary_input["note"]


def _lanes_after_a_mid_scan_loss() -> dict:
    """The previous run read every lane up, then found some of their services Down."""
    on_ports = {"s1": LANE0, "s2": LANE1, "s3": LANE1, "s4": "HundredGigE0/0/0/5", "s5": "HundredGigE0/0/0/6"}
    older = _ports_scan(on_ports, {port: "up" for port in on_ports.values()})
    older["diagnoses"] = [{"kind": "dataplane", "subject": {"name": name, "service_type": "l2bridge"},
                           "status": status}
                          for name, status in (("s1", "down"), ("s2", "down"), ("s3", "up"), ("s5", "degraded"))]
    # A fault carried from an earlier run is not one the previous run found.
    older["last_known_service_faults"] = [{"service": "s4", "status": "down"}]
    newer = _ports_scan(on_ports, {port: "down" for port in on_ports.values()})
    return compute_case_delta(older, newer)


def test_lane_whose_services_the_previous_run_already_found_down_is_marked():
    marked = {row["interface"]: (row["services_down_before"], row["services_on_port"])
              for row in _lanes_after_a_mid_scan_loss()["interfaces_lost"]}

    assert marked == {LANE0: (1, 1), LANE1: (1, 2), "HundredGigE0/0/0/5": (0, 1), "HundredGigE0/0/0/6": (0, 1)}


def test_changes_section_and_summary_input_say_the_loss_was_already_reported():
    from diagnostic_mas.case_delta import compact_delta_for_llm, format_changes_since_previous

    delta = _lanes_after_a_mid_scan_loss()
    delta["interfaces_lost"].append({"device": "pe2-data-sw", "interface": "HundredGigE0/0/0/7", "now": "down",
                                     "sub_interfaces": 0, "services_deleted": 0,
                                     "services_down_before": 2, "services_on_port": 2})
    text = "\n".join(format_changes_since_previous(delta, previous_run_id="run-1"))
    summary_input = compact_delta_for_llm(delta)["interfaces_lost"]

    line = (f"`pe1-data-sw`: HundredGigE0/0/0/5, HundredGigE0/0/0/6, "
            f"{LANE0} (its service was already Down in the previous run), "
            f"{LANE1} (1 of its 2 services was already Down in the previous run)")
    assert f"- {line}" in text
    assert "- `pe2-data-sw`: HundredGigE0/0/0/7 (its 2 services were already Down in the previous run)" in text
    assert line.replace("`", "") in summary_input["by_device"]
    assert "already Down in the previous run" in summary_input["note"]


def test_interface_is_not_marked_when_the_newer_scan_was_narrowed():
    older = _ports_scan({"s1": LANE0}, {LANE0: "up"})
    newer = _ports_scan({}, {LANE0: "down"})
    newer["run_configuration"]["Device filter"] = "pe1-data-sw"

    assert [row["services_deleted"] for row in compute_case_delta(older, newer)["interfaces_lost"]] == [0]

def _fault_then(newer_status: str | None, *, checked: bool = True) -> tuple[dict, dict]:
    """Older run: `svc` is a last-known Down. Newer run: its fixed check gives ``newer_status``."""
    older = _scan({"svc": "l3rt", "other": "l3rt"})
    older["last_known_service_faults"] = [{
        "service": "svc", "status": "down", "service_type": "l3rt",
        "last_observed_run_id": "r1", "cause": "uplink down"}]
    newer = _scan({"svc": "l3rt", "other": "l3rt"})
    record = newer["evidence"][0]["payload"]["extra"]["services"]["l3rt/svc"]
    record.update({"in_sync": True, "device_sync": {"pe1": "in-sync"}})
    if checked:
        record["basic_checks"] = {"status": newer_status, "checks": []}
    return older, newer


def test_last_known_fault_ends_when_the_service_passes_the_fixed_checks_this_run():
    from diagnostic_mas.case_delta import service_fault_history, format_changes_since_previous

    older, newer = _fault_then("up")

    assert service_fault_history(older, newer) == []
    delta = compute_case_delta(older, newer)
    assert delta["last_known_service_faults"] == []
    assert delta["service_faults_cleared"] == [
        {"service": "svc", "service_type": "l3rt", "was": "down", "last_observed_run_id": "r1"}]
    text = "\n".join(format_changes_since_previous(delta, previous_run_id="r1"))
    assert "- `l3rt/svc` — passes the checks this run (was down in run `r1`)" in text
    assert "Last-known service faults" not in text


def test_last_known_fault_is_kept_without_a_passing_check_this_run():
    from diagnostic_mas.case_delta import service_fault_history

    for why, (older, newer) in {
        "fixed check still unknown": _fault_then("unknown"),
        "fixed check still down": _fault_then("down"),
        "not checked this run": _fault_then(None, checked=False),
    }.items():
        history = service_fault_history(older, newer)
        assert [row["service"] for row in history] == ["svc"], why
        assert compute_case_delta(older, newer)["service_faults_cleared"] == [], why


def test_passing_check_does_not_end_a_fault_the_llm_confirmed_again_this_run():
    from diagnostic_mas.case_delta import service_fault_history

    older, newer = _fault_then("up")
    newer["diagnoses"] = [{"kind": "dataplane", "source": "llm", "status": "down", "complete": True,
                           "subject": {"name": "svc", "service_type": "l3rt"},
                           "observed": "o", "cause": "c"}]

    assert [row["service"] for row in service_fault_history(older, newer)] == ["svc"]



ESNET = {"device": "star-data-sw", "address": "198.125.141.24", "remote_as": 293, "outside": True}


def _bgp_scan(rows: list[dict], issues: list[dict] = ()) -> dict:
    """A saved case whose BGP spine stored these unmatched summary rows."""
    return {"evidence": [{"kind": "spine", "role": "bgp", "layer": "routing",
                          "payload": {"operational_edges": [], "extra": {"unmatched_neighbors": rows}}}],
            "issues": list(issues), "diagnoses": [], "service_coverage": {}, "focus_devices": []}


def test_outside_session_that_stopped_being_established_is_listed():
    from diagnostic_mas.case_delta import compact_delta_for_llm, format_changes_since_previous

    older = _bgp_scan([ESNET | {"state": "established", "up_down": "1y08w"}])
    delta = compute_case_delta(older, _bgp_scan([ESNET | {"state": "idle", "up_down": "00:12:03"}]))
    text = "\n".join(format_changes_since_previous(delta, previous_run_id="run-1"))

    line = "BGP `star-data-sw` → 198.125.141.24 (outside peer, AS 293) — idle (this end only)"
    assert f"- {line}" in text
    assert line.replace("`", "") in compact_delta_for_llm(delta)["routing_sessions_lost"]["sessions"]
    # Still up, gone from the table, or never up before: nothing to report.
    for now in ([ESNET | {"state": "established"}], []):
        assert compute_case_delta(older, _bgp_scan(now))["routing_sessions_lost"] == []
    assert compute_case_delta(_bgp_scan([ESNET | {"state": "active"}]),
                              _bgp_scan([ESNET | {"state": "idle"}]))["routing_sessions_lost"] == []


def test_neighbor_now_sorted_as_an_outside_peer_is_not_called_recovered():
    unmatched = {"code": "unknown_neighbor_address", "layer": "routing", "status": "open",
                 "message": "star-data-sw 198.125.141.24: could not map neighbor address to NSO device"}
    still_unknown = dict(unmatched, message="wash-data-sw 10.128.128.1: could not map neighbor address to NSO device")
    older = _bgp_scan([], [unmatched, still_unknown])

    delta = compute_case_delta(older, _bgp_scan([ESNET | {"state": "established"}]))

    # Re-sorted, not fixed; one that simply went away is still a recovery.
    assert [i["message"] for i in delta["recovered"]] == [still_unknown["message"]]


def test_summary_input_says_what_each_new_problem_is():
    from diagnostic_mas.case_delta import compact_delta_for_llm

    peering = {"code": "outside_bgp_session_not_established", "layer": "routing", "status": "open",
               "edge_id": "bgp-outside:star-data-sw:10.129.134.2", "devices": ["star-data-sw"],
               "message": ("star-data-sw 10.129.134.2 (outside peer, AS 47065): Active "
                           "(Up/Down 00:00:00); only this end is visible")}
    row = ESNET | {"address": "10.129.134.2", "remote_as": 47065, "state": "active"}

    summary_input = compact_delta_for_llm(compute_case_delta(_bgp_scan([]), _bgp_scan([row], [peering])))

    new = summary_input["new_problems"]
    assert new["count"] == 1
    assert [(p["code"], p["message"]) for p in new["sample"]] == [(peering["code"], peering["message"])]
    assert "does not prove a new failure" in new["note"]
    assert "new_problems" not in compact_delta_for_llm(compute_case_delta(_bgp_scan([]), _bgp_scan([])))


def _with_lane(case, links: dict, ages: dict):
    from diagnostic_mas.case import add_evidence

    add_evidence(case, {"kind": "port_investigation", "role": "port", "layer": "physical", "payload": {
        "device": "fiu-data-sw", "port": "HundredGigE0/0/0/24", "concluded": True, "links": links, "link_ages": ages}})
    return case


def test_a_newly_detected_fault_names_its_lane_and_whether_that_lane_was_down_before():
    from diagnostic_mas.case_delta import compact_delta_for_llm, format_changes_since_previous

    lane0, lane1 = "HundredGigE0/0/0/24/0", "HundredGigE0/0/0/24/1"
    before = _with_lane(_dp_case(), {lane0: ["deleted-since"]}, {lane0: "2d01h"})   # its owner replaced the service

    # A new service on a lane that was already down.
    delta = compute_case_delta(case_to_dict(before),
                               case_to_dict(_with_lane(_dp_case("down"), {lane0: ["circuit"]}, {lane0: "2d03h"})))
    note = f"; on fiu-data-sw {lane0}, last flapped 2d03h ago; that lane was down in the previous run too"
    assert delta["newly_failed_services"][0]["detail"].endswith(note)
    assert note in "\n".join(format_changes_since_previous(delta))
    assert compact_delta_for_llm(delta)["newly_failed_services"][0]["detail"].endswith(note)

    # A lane newly down: its age only.
    delta = compute_case_delta(case_to_dict(before),
                               case_to_dict(_with_lane(_dp_case("down"), {lane1: ["circuit"]}, {lane1: "02:15:40"})))
    assert delta["newly_failed_services"][0]["detail"].endswith(f"; on fiu-data-sw {lane1}, last flapped 02:15:40 ago")

    # Not explained by a port investigation: unchanged.
    delta = compute_case_delta(case_to_dict(_dp_case()), case_to_dict(_dp_case("down")))
    assert "last flapped" not in delta["newly_failed_services"][0]["detail"]


def test_the_summary_gets_links_gaining_errors_one_line_per_link():
    from diagnostic_mas.case_delta import compact_delta_for_llm

    def end(device, port, far, far_port, crc):
        return {"device": device, "port": port, "description": "", "gained": {"crc": crc}, "state_changes": 18,
                "minutes": 792, "services": 0, "adjacency": True, "device_services": 74,
                "far_end": {"device": far, "interface": far_port}}

    rows = [end("salt-data-sw", "HundredGigE0/0/0/17", "kans-data-sw", "HundredGigE0/0/0/23.3000", 5),
            end("kans-data-sw", "HundredGigE0/0/0/23", "salt-data-sw", "HundredGigE0/0/0/17.3000", 7)]
    sent = compact_delta_for_llm({"links_gaining_errors": rows})["links_gaining_errors"]

    assert sent["count"] == 1 and len(sent["links"]) == 1
    assert sent["links"][0].startswith("salt-data-sw HundredGigE0/0/0/17 ↔ kans-data-sw HundredGigE0/0/0/23, one IS-IS link")
    assert "not a fault" in sent["note"]
    assert "links_gaining_errors" not in compact_delta_for_llm({})
