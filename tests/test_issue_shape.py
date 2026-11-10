"""Issues have one shape, written down once and given to issues from outside a run where they enter."""
import json

import pytest

from diagnostic_mas.case import ISSUE_FIELDS, Budget, CaseFile, open_issue
from diagnostic_mas.case_delta import compute_case_delta, issue_key, load_case_dict


def test_an_issue_the_scan_opens_has_every_field_of_the_shape():
    case = CaseFile(budget=Budget(0, 0))
    open_issue(case, code="session_down", message="BGP session down", evidence_ids=[])

    assert set(ISSUE_FIELDS) <= set(case.issues[0])


def test_a_saved_issue_missing_fields_is_given_the_shape_when_loaded(tmp_path):
    path = tmp_path / "case.json"
    path.write_text(json.dumps({"issues": [
        {"code": "collection_error", "message": "x-data-sw: read failed", "status": None},
        "not an issue",
    ]}))

    assert load_case_dict(path)["issues"] == [
        {"id": "", "code": "collection_error", "message": "x-data-sw: read failed", "evidence_ids": [],
         "severity": "medium", "layer": None, "edge_id": None, "status": "open"}]


def test_the_delta_gives_hand_made_issues_the_shape_before_reading_them():
    older = {"issues": [{"code": "session_down", "layer": "routing", "edge_id": "bgp:a"}]}
    newer = {"issues": [{"code": "session_down", "layer": "routing", "edge_id": "bgp:a"},
                        {"code": "collection_error", "message": "y: failed", "status": "resolved"}]}

    delta = compute_case_delta(older, newer)

    assert [issue["edge_id"] for issue in delta["persistent"]] == ["bgp:a"]
    assert delta["new_problems"] == []


def test_a_reader_given_a_dict_without_the_shape_fails_loudly():
    with pytest.raises(KeyError):
        issue_key({"message": "no code, layer or edge"})


def test_a_conclusion_the_scan_records_has_every_field_of_the_diagnosis_shape():
    from diagnostic_mas.case import DIAGNOSIS_FIELDS
    from diagnostic_mas.dataplane_verify import _commit_dataplane_conclusion, parse_dataplane_conclusion

    case = CaseFile(budget=Budget(0, 0))
    for record, status in (({"name": "svc-a", "service_type": None, "system_status": "up"}, "down"),
                           ({"name": "svc-b", "service_type": "l2sts", "system_status": "up"}, "unknown")):
        conclusion = parse_dataplane_conclusion({"dataplane_status": status, "observed": "read", "cause": "why"})
        _commit_dataplane_conclusion(case, record, conclusion, source="llm")

    assert all(set(DIAGNOSIS_FIELDS) <= set(dx) for dx in case.diagnoses)
    assert [(dx["subject"], dx["status"], dx["complete"]) for dx in case.diagnoses] == [
        ({"service_type": None, "name": "svc-a"}, "down", True),
        ({"service_type": "l2sts", "name": "svc-b"}, "unknown", False)]


def test_a_saved_diagnosis_is_given_the_shape_when_loaded(tmp_path):
    path = tmp_path / "case.json"
    path.write_text(json.dumps({"diagnoses": [
        {"kind": "dataplane", "source": "llm", "status": "down", "observed": "o", "cause": "c",
         "subject": {"service_type": "l3rt", "name": "svc-a"}, "fix_suggestion": "reseat"},
        {"kind": "dataplane", "name": "svc-old", "dataplane_status": "up", "observed": "o", "cause": "c"},
        {"kind": "dataplane", "subject": {}, "status": "up"},
        "not a diagnosis",
    ]}))

    assert load_case_dict(path)["diagnoses"] == [
        {"id": "", "kind": "dataplane", "source": "llm", "observed": "o", "cause": "c", "evidence_ids": [],
         "status": "down", "subject": {"service_type": "l3rt", "name": "svc-a"}, "confidence": "medium",
         "complete": True, "fix_suggestion": "reseat"},
        {"id": "", "kind": "dataplane", "source": "", "observed": "o", "cause": "c", "evidence_ids": [],
         "status": "up", "subject": {"service_type": None, "name": "svc-old"}, "confidence": "medium",
         "complete": True}]


def test_the_delta_gives_hand_made_diagnoses_the_shape_before_reading_them():
    newer = {"diagnoses": [{"kind": "dataplane", "subject": {"name": "svc", "service_type": "l3rt"},
                            "status": "down"}]}

    delta = compute_case_delta({}, newer)

    assert [row["service"] for row in delta["newly_failed_services"]] == ["svc"]


def test_a_reader_given_a_diagnosis_without_the_shape_fails_loudly():
    from diagnostic_mas.report import _dataplane_dig_subjects

    case = CaseFile(budget=Budget(0, 0))
    case.diagnoses.append({"kind": "dataplane", "status": "down"})

    with pytest.raises(KeyError):
        _dataplane_dig_subjects(case)


def test_a_saved_service_record_is_given_the_record_shape():
    from diagnostic_mas.case import normalize_service_record

    assert normalize_service_record("l2sts/svc-a", {"devices": ["pe1"], "basic_checks": {"intent": None}}) == {
        "name": "svc-a", "service_type": "l2sts", "devices": ["pe1"],
        "basic_checks": {"status": "not_checked", "checks": [], "intent": None}}
    # Older scans have records without fixed checks: that stays visible, not defaulted.
    assert normalize_service_record("l2bridge/svc-b", {"name": "svc-b"}) == {
        "name": "svc-b", "service_type": "l2bridge"}
    assert normalize_service_record("l3rt/svc-c", "not a record") is None


def test_the_delta_gives_hand_made_service_records_the_shape_before_reading_them():
    full = {"Device filter": "All", "Service type filter": "All", "Service ID filter": "All", "Scope flags": "Default"}

    def scan(records):
        return {"run_configuration": full,
                "evidence": [{"kind": "spine", "role": "service", "payload": {"extra": {"services": records}}}]}

    older = scan({"l3rt/kept": {"devices": ["pe1"]}, "l3rt/gone": {"devices": ["pe1"]}})
    delta = compute_case_delta(older, scan({"l3rt/kept": {"devices": ["pe1"]}}))

    assert delta["services_no_longer_present"] == [{"service": "gone", "service_type": "l3rt", "previous_coverage": ""}]


def _case_listing(records):
    case = CaseFile(budget=Budget(0, 0))
    case.evidence.append({"kind": "spine", "role": "service", "payload": {"extra": {"services": records}}})
    return case


def test_every_way_into_service_records_gives_the_record_shape():
    from diagnostic_mas.dataplane_verify import iter_service_records
    from diagnostic_mas.device_health import services_from_case

    def listed():
        return _case_listing({"l2sts/svc-a": {"devices": ["pe1"], "basic_checks": {"status": "down"}},
                              "l3rt/svc-b": "not a record"})

    shaped = {"name": "svc-a", "service_type": "l2sts", "devices": ["pe1"],
              "basic_checks": {"status": "down", "checks": []}}
    assert [record for _, record in iter_service_records(listed())] == [shaped]
    assert services_from_case(listed()) == {"l2sts/svc-a": shaped}


def test_a_record_is_shaped_where_the_case_keeps_it_so_what_a_caller_writes_stays():
    from diagnostic_mas.case import service_records

    case = _case_listing({"l3rt/svc": {"devices": ["pe1"]}})
    [(_, key, record)] = service_records(case.evidence)
    record["dataplane_status"] = "down"

    assert key == "l3rt/svc"
    assert case.evidence[0]["payload"]["extra"]["services"]["l3rt/svc"] is record
