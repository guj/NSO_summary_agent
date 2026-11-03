"""History across scans: per-scan outcomes, the carried record, and the clause the report adds."""

from diagnostic_mas.item_history import (
    CLEAR, SEEN, UNREAD, change_key, link_key, recurrence_clause, routing_key, scan_outcomes, update_history)

HUB, PE = "hub-data-sw", "pe1-data-sw"
BGP = f"bgp:10.0.0.1:10.0.1.1:{HUB}:{PE}"
OUTSIDE = f"bgp-outside:{HUB}:10.9.9.9"
ISIS = f"isis:{HUB}:HundredGigE0/0/0/1.3000:{PE}:HundredGigE0/0/0/2.3000"
ISIS_2 = f"isis:{HUB}:HundredGigE0/0/0/3.3000:{PE}:HundredGigE0/0/0/4.3000"
ISIS_3 = f"isis:{HUB}:HundredGigE0/0/0/5.3000:{PE}:HundredGigE0/0/0/6.3000"
UP = {"local": "up", "remote": "up", "status": "up"}
ESTABLISHED = {"local": "established", "remote": "established", "status": "up"}
IDLE = {"local": "idle", "remote": "idle", "status": "down"}
UNKNOWN = {"local": "unknown", "remote": "unknown", "status": "unknown"}


def _issue(code: str, edge: str) -> dict:
    return {"code": code, "edge_id": edge, "layer": "routing", "status": "open"}


def _port(**changed) -> dict:
    return {"state": "up", "transitions": 26, "cleared": "never", "input_errors": 0, "crc": 0,
            "output_errors": 0, "input_drops": 0, "output_drops": 0, "services": 0, "adjacency": False,
            "device_services": 0} | changed


def _scan(run_id: str | None = None, *, bgp: dict | None = None, isis: dict | None = None,
          issues: tuple = (), outside: list | None = None, ports: dict | None = None,
          read_at: str | None = None, changed: dict | None = None) -> dict:
    """A saved case: routing edges by protocol (edge id -> state), issues, outside rows and port readings."""
    evidence = []
    for role, edges in (("bgp", bgp), ("isis", isis)):
        if edges is None:
            continue
        payload = {"operational_edges": [
            {"id": edge, "state": dict(state), **({"changed_at": changed[edge]} if edge in (changed or {}) else {})}
            for edge, state in edges.items()]}
        if role == "bgp" and outside is not None:
            payload["extra"] = {"unmatched_neighbors": outside}
        evidence.append({"kind": "spine", "role": role, "payload": payload})
    if ports is not None:
        evidence.append({"kind": "port_counters", "role": "physical",
                         "payload": {"read_at": read_at, "ports": ports}})
    case = {"evidence": evidence, "issues": list(issues), "device_names": [HUB, PE]}
    return case | ({"run_id": run_id} if run_id else {})


def _entry(**changed) -> dict:
    return {"first_seen_run_id": "20261027T185052Z", "first_seen_exact": False, "last_seen_run_id": "20261102T234904Z",
            "seen": 28, "read": 28, "scans": 28, "recent": SEEN * 20} | changed


def test_clause_wording():
    every = _entry()
    assert recurrence_clause(every, "gaining errors") == "gaining errors in every scan since 27 Oct or earlier (28)"
    gaps = _entry(first_seen_exact=True, seen=3, read=3, scans=4, recent="SS-S")
    assert recurrence_clause(gaps, "down") == "down in every scan that read it since 27 Oct (3)"
    some = _entry(first_seen_exact=True, seen=7, read=20, scans=20, recent="SCC" * 6 + "SC")
    assert recurrence_clause(some, "changed state") == (
        "changed state in 7 of the last 20 scans that read it; first seen 27 Oct")
    assert recurrence_clause(_entry(seen=1, read=1, scans=1, recent="S"), "down") is None
    assert recurrence_clause(None, "down") is None
    assert recurrence_clause(gaps, "down", dated=False) == "down in every scan that read it (3)"
    assert recurrence_clause(some, "down", dated=False) == "down in 7 of the last 20 scans that read it"


def test_open_routing_fault_is_seen_and_session_read_up_is_clear():
    down = _scan(bgp={BGP: IDLE}, issues=(_issue("configured_no_session", BGP),))
    assert scan_outcomes(None, down) == {routing_key(BGP): SEEN}
    assert scan_outcomes(down, _scan(bgp={BGP: ESTABLISHED}), [routing_key(BGP)]) == {routing_key(BGP): CLEAR}

    row = {"device": HUB, "address": "10.9.9.9", "outside": True}
    outside_down = _scan(bgp={}, outside=[row | {"state": "active"}],
                         issues=(_issue("outside_bgp_session_not_established", OUTSIDE),))
    assert scan_outcomes(None, outside_down) == {routing_key(OUTSIDE): SEEN}
    outside_up = _scan(bgp={}, outside=[row | {"state": "established"}])
    assert scan_outcomes(None, outside_up, [routing_key(OUTSIDE)]) == {routing_key(OUTSIDE): CLEAR}


def test_verification_incomplete_is_unread_not_clear():
    history = {routing_key(BGP): _entry()}
    incomplete = _scan(bgp={BGP: UNKNOWN}, issues=(_issue("bgp_verification_incomplete", BGP),))

    outcomes = scan_outcomes(None, incomplete, history)
    updated = update_history(history, outcomes, "20261103T041450Z", previous=None)

    assert outcomes == {routing_key(BGP): UNREAD}
    assert updated[routing_key(BGP)] == _entry(scans=29, recent=SEEN * 19 + UNREAD)


def test_session_up_at_the_only_end_read_is_clear():
    tracked = [routing_key(BGP)]
    one_end = _scan(bgp={BGP: {"local": "established", "remote": "unknown", "status": "unknown"}})

    assert scan_outcomes(None, one_end, tracked) == {routing_key(BGP): CLEAR}
    assert scan_outcomes(None, _scan(bgp={BGP: UNKNOWN}), tracked) == {routing_key(BGP): UNREAD}
    assert scan_outcomes(None, _scan(), tracked) == {routing_key(BGP): UNREAD}


def test_link_and_adjacency_outcomes_come_from_the_two_comparisons():
    ports = {f"{HUB}:HundredGigE0/0/0/1": _port(), f"{HUB}:HundredGigE0/0/0/2": _port()}
    older = _scan("20261102T234904Z", isis={ISIS: UP, ISIS_2: UP}, ports=ports, read_at="2026-11-02T23:50:00+00:00")
    newer = _scan(isis={ISIS: UP, ISIS_2: UP},
                  changed={ISIS: "2026-11-03T01:00:00+00:00", ISIS_2: "2026-10-30T00:00:00+00:00"},
                  ports={f"{HUB}:HundredGigE0/0/0/1": _port(crc=7), f"{HUB}:HundredGigE0/0/0/2": _port()},
                  read_at="2026-11-03T02:00:00+00:00")
    tracked = [link_key(HUB, "HundredGigE0/0/0/2"), link_key(HUB, "HundredGigE0/0/0/3"),
               change_key(ISIS_2), change_key(ISIS_3)]

    assert scan_outcomes(older, newer, tracked) == {
        link_key(HUB, "HundredGigE0/0/0/1"): SEEN, link_key(HUB, "HundredGigE0/0/0/2"): CLEAR,
        link_key(HUB, "HundredGigE0/0/0/3"): UNREAD,
        change_key(ISIS): SEEN, change_key(ISIS_2): CLEAR, change_key(ISIS_3): UNREAD}


def test_new_item_is_exact_only_when_the_previous_scan_read_it():
    now = _scan(bgp={BGP: IDLE}, issues=(_issue("configured_no_session", BGP),))

    def first(previous):
        history = update_history({}, scan_outcomes(previous, now), "20261103T041450Z", previous=previous)
        return history[routing_key(BGP)]

    assert first(_scan(bgp={BGP: ESTABLISHED})) == {
        "first_seen_run_id": "20261103T041450Z", "first_seen_exact": True, "last_seen_run_id": "20261103T041450Z",
        "seen": 1, "read": 1, "scans": 1, "recent": SEEN}
    assert first(None)["first_seen_exact"] is False
    assert first(_scan(bgp={BGP: UNKNOWN}))["first_seen_exact"] is False
    # Read, but not up: the fault may already have been there.
    assert first(_scan(bgp={BGP: {"local": "idle", "remote": "unknown", "status": "unknown"}}))[
        "first_seen_exact"] is False


def test_a_link_first_seen_right_after_an_uncompared_scan_is_not_exact(tmp_path):
    from diagnostic_mas.item_history import backfill_history

    def counters(read_at: str, crc: int) -> dict:
        return _scan(ports={f"{HUB}:HundredGigE0/0/0/1": _port(crc=crc)}, read_at=read_at)

    # The first saved scan has nothing before it to compare with, so the second cannot date the gain.
    _save(tmp_path, "20261101T000000Z", counters("2026-11-01T00:00:00+00:00", 5))
    _save(tmp_path, "20261101T020000Z", counters("2026-11-01T02:00:00+00:00", 9))
    _save(tmp_path, "20261101T040000Z", counters("2026-11-01T04:00:00+00:00", 14))

    entry = backfill_history(tmp_path, before="20261102T000000Z")[link_key(HUB, "HundredGigE0/0/0/1")]

    assert (entry["first_seen_run_id"], entry["first_seen_exact"], entry["seen"]) == ("20261101T020000Z", False, 2)


def test_item_not_seen_for_a_full_window_is_dropped():
    gone = {routing_key(BGP): _entry(recent=SEEN + UNREAD * 19)}
    assert update_history(gone, {}, "20261103T041450Z", previous=None) == {}

    quiet = {routing_key(BGP): _entry(recent=SEEN + CLEAR * 18)}
    kept = update_history(quiet, {routing_key(BGP): CLEAR}, "20261103T041450Z", previous=None)
    assert kept[routing_key(BGP)]["recent"] == SEEN + CLEAR * 19


def _save(runs, run_id: str, case: dict | str) -> None:
    folder = runs / run_id
    folder.mkdir(parents=True)
    import json

    (folder / "case.json").write_text(case if isinstance(case, str) else json.dumps(case | {"run_id": run_id}))


def test_backfill_walks_saved_scans_oldest_first(tmp_path):
    from diagnostic_mas.item_history import backfill_history

    down = _scan(bgp={BGP: IDLE}, issues=(_issue("configured_no_session", BGP),))
    _save(tmp_path, "20261101T000000Z", _scan(bgp={BGP: ESTABLISHED}))
    _save(tmp_path, "20261101T020000Z", down)
    _save(tmp_path, "20261101T040000Z", down)
    _save(tmp_path, "20261101T060000Z", down)   # not before the scan being run: left out

    history = backfill_history(tmp_path, before="20261101T060000Z")

    assert history == {routing_key(BGP): {
        "first_seen_run_id": "20261101T020000Z", "first_seen_exact": True, "last_seen_run_id": "20261101T040000Z",
        "seen": 2, "read": 2, "scans": 2, "recent": SEEN * 2}}


def test_backfill_reads_only_the_most_recent_saved_scans(tmp_path):
    from diagnostic_mas.item_history import backfill_history

    down = _scan(bgp={BGP: IDLE}, issues=(_issue("configured_no_session", BGP),))
    for hour in range(5):
        _save(tmp_path, f"20261101T0{hour}0000Z", down)

    entry = backfill_history(tmp_path, before="20261102T000000Z", limit=3)[routing_key(BGP)]

    assert (entry["first_seen_run_id"], entry["first_seen_exact"], entry["seen"]) == ("20261101T020000Z", False, 3)


def test_backfill_restarts_after_an_unreadable_scan(tmp_path):
    from diagnostic_mas.item_history import backfill_history

    down = _scan(bgp={BGP: IDLE}, issues=(_issue("configured_no_session", BGP),))
    _save(tmp_path, "20261101T000000Z", down)
    _save(tmp_path, "20261101T020000Z", "{")
    _save(tmp_path, "20261101T040000Z", down)

    entry = backfill_history(tmp_path, before="20261102T000000Z")[routing_key(BGP)]

    assert (entry["first_seen_run_id"], entry["first_seen_exact"], entry["seen"]) == ("20261101T040000Z", False, 1)


def test_backfill_without_a_runs_folder_is_empty(tmp_path):
    from diagnostic_mas.item_history import backfill_history

    assert backfill_history(tmp_path / "runs", before="20261102T000000Z") == {}


def test_history_for_scan_carries_the_saved_record_without_backfill(tmp_path, monkeypatch):
    from diagnostic_mas import item_history

    def no_backfill(*_args, **_kwargs):
        raise AssertionError("the previous case has a record")

    monkeypatch.setattr(item_history, "backfill_history", no_backfill)
    down = _scan(bgp={BGP: IDLE}, issues=(_issue("configured_no_session", BGP),))
    previous = down | {"run_id": "20261102T234904Z", "item_history": {routing_key(BGP): _entry()}}

    history = item_history.history_for_scan(previous, down, runs_dir=tmp_path, run_id="20261103T041450Z")

    assert history[routing_key(BGP)]["seen"] == 29


def test_history_for_scan_builds_a_missing_record_from_the_saved_scans(tmp_path):
    from diagnostic_mas.item_history import history_for_scan

    down = _scan(bgp={BGP: IDLE}, issues=(_issue("configured_no_session", BGP),))
    _save(tmp_path, "20261101T000000Z", _scan(bgp={BGP: ESTABLISHED}))
    _save(tmp_path, "20261101T020000Z", down)

    history = history_for_scan(down | {"run_id": "20261101T020000Z"}, down, runs_dir=tmp_path,
                               run_id="20261101T040000Z")

    assert (history[routing_key(BGP)]["first_seen_run_id"], history[routing_key(BGP)]["seen"]) == (
        "20261101T020000Z", 2)
    assert history_for_scan(None, down, runs_dir=tmp_path, run_id="20261101T040000Z")[routing_key(BGP)]["seen"] == 1


def test_case_to_dict_saves_item_history():
    from diagnostic_mas.case import Budget, CaseFile
    from diagnostic_mas.state_paths import case_to_dict

    case = CaseFile(budget=Budget(0, 0))
    case.item_history = {routing_key(BGP): _entry()}

    assert case_to_dict(case)["item_history"] == {routing_key(BGP): _entry()}


TOKY_PORT = "TenGigE0/0/0/18/0"
TOKY_CHANGE = "isis:seat-data-sw:HundredGigE0/0/0/19.1491:toky-data-sw:TenGigE0/0/0/18/0.2175"
SALT_SEAT = "isis:salt-data-sw:?:seat-data-sw:HundredGigE0/0/0/23.3000"


def _gain_row() -> dict:
    return {"device": "toky-data-sw", "port": TOKY_PORT, "description": "", "gained": {"crc": 8290, "input_errors": 8290},
            "state_changes": 0, "minutes": 141, "services": 0, "adjacency": True, "device_services": 32,
            "far_end": {"device": "seat-data-sw", "interface": "HundredGigE0/0/0/19.1491"}}


def _changes_delta(**extra) -> dict:
    return {"links_gaining_errors": [_gain_row()],
            "adjacencies_changed_between_scans": [
                {"protocol": "isis", "edge_id": TOKY_CHANGE, "changed_at": "2026-11-03T04:11:00+00:00"}]} | extra


HISTORY = {link_key("toky-data-sw", TOKY_PORT): _entry(),
           change_key(TOKY_CHANGE): _entry(first_seen_run_id="20261029T180948Z", seen=16, read=16, scans=16),
           routing_key(SALT_SEAT): _entry(seen=29, read=29, scans=29)}


def test_changes_section_puts_age_on_link_and_adjacency_lines():
    from diagnostic_mas.case_delta import format_changes_since_previous

    text = "\n".join(format_changes_since_previous(_changes_delta(item_history=HISTORY)))

    assert ("- `toky-data-sw` TenGigE0/0/0/18/0 (IS-IS link to `seat-data-sw` HundredGigE0/0/0/19.1491): +8290 CRC, "
            "+8290 input errors in 2 h 21 min; 32 services on this device; "
            "listed in every scan since 27 Oct or earlier (28)") in text
    assert ("- IS-IS `seat-data-sw` HundredGigE0/0/0/19.1491 ↔ `toky-data-sw` TenGigE0/0/0/18/0.2175 — "
            "last changed 3 Nov 04:11 UTC; changed state in every scan since 29 Oct or earlier (16)") in text


def test_no_history_leaves_the_lines_as_they_were():
    from diagnostic_mas.case_delta import _adjacency_change_line, format_changes_since_previous
    from diagnostic_mas.port_counters import gain_lines

    delta = _changes_delta()
    text = "\n".join(format_changes_since_previous(delta))

    assert f"- {gain_lines(delta['links_gaining_errors'])[0]}\n" in text
    assert text.count(" in every scan") == 0
    assert f"- {_adjacency_change_line(delta['adjacencies_changed_between_scans'][0])}\n" in text


def test_persistent_routing_problem_carries_its_age():
    from diagnostic_mas.case_delta import format_changes_since_previous

    issue = {"code": "unidirectional_adjacency", "edge_id": SALT_SEAT, "layer": "underlay", "status": "open",
             "message": "IS-IS adjacency down: salt-data-sw does not list it"}
    text = "\n".join(format_changes_since_previous({"persistent": [issue], "item_history": HISTORY}))

    assert "does not list it; down in every scan since 27 Oct or earlier (29)\n" in text + "\n"


def test_digest_and_summary_input_carry_the_age():
    import json

    from diagnostic_mas.case_delta import compact_delta_for_llm
    from diagnostic_mas.open_faults import digest_changes

    issue = {"code": "unidirectional_adjacency", "edge_id": SALT_SEAT, "layer": "underlay", "status": "open"}
    delta = _changes_delta(item_history=HISTORY, persistent=[issue])
    clause = "listed in every scan since 27 Oct or earlier (28)"

    assert any(line.endswith(clause) for line in digest_changes(delta, "20261102T234904Z", []))
    summary_input = compact_delta_for_llm(delta)
    assert clause in json.dumps(summary_input, ensure_ascii=False)
    assert summary_input["persistent_sample"][0]["recurrence"] == "down in every scan since 27 Oct or earlier (29)"


def _down_case(code: str, edge: str, message: str):
    from diagnostic_mas.case import Budget, CaseFile, open_issue

    case = CaseFile(budget=Budget(0, 0))
    parts = edge.split(":")
    case.device_names = sorted({parts[1], parts[3]})
    open_issue(case, code=code, severity="high", layer="underlay", evidence_ids=[], edge_id=edge,
               devices=[parts[1], parts[3]], message=message)
    return case


def test_held_adjacency_with_missing_since_gets_count_only():
    from diagnostic_mas.operator_report import format_devices_operator

    edge = "isis:star-data-sw:FourHundredGigE0/0/0/60.300:cien-data-sw:?"
    case = _down_case("configured_no_adjacency", edge,
                      "IS-IS adjacency down: star-data-sw does not list it; cien-data-sw not read this scan "
                      "(missing since 30 Oct).")
    case.item_history = {routing_key(edge): _entry(first_seen_exact=True, seen=5, read=5, scans=6)}

    attention = [line for line in format_devices_operator(case) if line.startswith("**Attention:** IS-IS")]

    assert attention and all(line.endswith(" Down in every scan that read it (5).") for line in attention)


def test_device_block_shows_age_on_adjacency_attention_and_finding_line():
    from diagnostic_mas.case import add_evidence
    from diagnostic_mas.operator_report import format_devices_operator

    case = _down_case("unidirectional_adjacency", SALT_SEAT, "IS-IS adjacency down: salt-data-sw does not list it.")
    add_evidence(case, {"kind": "drill_finding", "role": "drill", "layer": "underlay", "payload": {
        "issue_edge_id": SALT_SEAT, "headline": "salt never classifies the tagged frames", "confidence": "high"}})
    case.item_history = HISTORY

    text = "\n".join(format_devices_operator(case))

    assert "Down in every scan since 27 Oct or earlier (29)." in text
    assert "IS-IS adjacency salt-data-sw ↔ seat-data-sw (down in every scan since 27 Oct or earlier (29))" in text


def test_a_change_first_seen_after_a_scan_with_no_change_time_is_not_exact():
    now = _scan(isis={ISIS: UP}, changed={ISIS: "2026-10-29T17:52:47+00:00"})

    def first(previous):
        history = update_history({}, scan_outcomes(previous, now), "20261029T180948Z", previous=previous)
        return history[change_key(ISIS)]["first_seen_exact"]

    assert first(_scan("20261029T161520Z", isis={ISIS: UP})) is False
    assert first(_scan("20261029T161520Z", isis={ISIS: UP}, changed={ISIS: "2026-10-28T00:00:00+00:00"})) is True



def test_a_link_that_only_changed_state_is_not_said_to_gain_errors():
    from diagnostic_mas.port_counters import gain_lines

    flapping = _gain_row() | {"gained": {}, "state_changes": 9}

    line = gain_lines([flapping], HISTORY)[0]

    assert "9 state changes" in line and "gaining errors" not in line
    assert line.endswith("; listed in every scan since 27 Oct or earlier (28)")
