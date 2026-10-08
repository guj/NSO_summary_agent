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


def test_session_still_up_or_of_unknown_state_is_not_lost():
    for state in ({"local": "established", "remote": "established", "status": "up"},
                  {"local": "established", "remote": "unknown", "status": "unknown"}):
        delta = compute_case_delta(_routing_scan(bgp=BOTH), _routing_scan(bgp={BGP_EDGE: state}))
        assert delta["routing_sessions_lost"] == []


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
        {"device": "pe1-data-sw", "interface": "HundredGigE0/0/0/23", "now": "down", "sub_interfaces": 1}]


def test_sub_interface_down_under_a_working_port_and_a_shut_port_are_listed_as_such():
    older = _interface_scan({UPLINK: ("up", "up"), UPLINK + ".3000": ("up", "up"), ACCESS: ("up", "up")})
    newer = _interface_scan({UPLINK: ("up", "up"), UPLINK + ".3000": ("down", "down"),
                             ACCESS: ("admin-down", "admin-down")})

    assert compute_case_delta(older, newer)["interfaces_lost"] == [
        {"device": "pe1-data-sw", "interface": "HundredGigE0/0/0/23.3000", "now": "down", "sub_interfaces": 0},
        {"device": "pe1-data-sw", "interface": "HundredGigE0/0/0/5", "now": "admin-down", "sub_interfaces": 0}]


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

