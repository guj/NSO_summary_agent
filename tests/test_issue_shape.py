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
