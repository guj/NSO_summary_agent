"""The notification leads with what changed since the previous scan and how old the open faults are."""
import json

from diagnostic_mas import port_investigation as pi
from diagnostic_mas.case import Budget, CaseFile, add_evidence
from diagnostic_mas.case_delta import (
    compute_case_delta, first_seen_from_saved_scans, format_changes_since_previous, service_fault_history)
from diagnostic_mas.html_report import notification_digest
from diagnostic_mas.open_faults import age_days, digest_changes, format_open_faults, open_fault_groups, run_day
from test_diagnostic_case_delta import _scan

R1, R2, R3 = "20261005T191030Z", "20261006T000518Z", "20261006T014943Z"
MICH, PORT = "mich-data-sw", "HundredGigE0/0/0/24"


def _faulted(*names: str, kind: str = "l2bridge") -> dict:
    scan = _scan({name: kind for name in ("ok", *names)})
    scan["issues"] = [{"layer": "services", "code": "service_down", "status": "open", "edge_id": name,
                       "message": "attachment down"} for name in names]
    return scan


def _next(older: dict, newer: dict, older_id: str, newer_id: str) -> dict:
    """``newer`` as it is saved: with the fault list worked out against ``older``."""
    newer["last_known_service_faults"] = service_fault_history(
        older, newer, previous_run_id=older_id, run_id=newer_id)
    return newer


# --- first seen ---------------------------------------------------------------

def test_fault_is_first_seen_in_the_scan_that_found_it_and_keeps_that_date_while_open():
    second = _next(_scan({"ok": "l2bridge", "bad": "l2bridge"}), _faulted("bad"), R1, R2)
    assert [(r["first_observed_run_id"], r["first_observed_exact"]) for r in
            second["last_known_service_faults"]] == [(R2, True)]

    third = _next(second, _faulted("bad"), R2, R3)
    row = third["last_known_service_faults"][0]
    assert (row["first_observed_run_id"], row["last_observed_run_id"]) == (R2, R3)


def test_fault_already_there_in_the_previous_scan_is_first_seen_then():
    rows = service_fault_history(_faulted("bad"), _faulted("bad"), previous_run_id=R1, run_id=R2)

    assert (rows[0]["first_observed_run_id"], rows[0]["last_observed_run_id"]) == (R1, R2)


def test_fault_that_cleared_and_came_back_is_first_seen_again():
    passing = _scan({"ok": "l2bridge", "bad": "l2bridge"})
    passing["evidence"][0]["payload"]["extra"]["services"]["l2bridge/bad"].update(
        {"in_sync": True, "device_sync": {"pe1": "in-sync"}, "basic_checks": {"status": "up", "checks": []}})
    second = _next(_faulted("bad"), passing, R1, R2)
    assert second["last_known_service_faults"] == []

    third = _next(second, _faulted("bad"), R2, R3)
    assert third["last_known_service_faults"][0]["first_observed_run_id"] == R3


def _save(runs, run_id: str, faults: list[dict] | None) -> None:
    (runs / run_id).mkdir(parents=True)
    case = {} if faults is None else {"last_known_service_faults": faults}
    (runs / run_id / "case.json").write_text(json.dumps(case))


def _legacy(name: str) -> dict:
    """A fault row as scans saved it before first seen was recorded."""
    return {"service": name, "status": "down", "service_type": "l2bridge", "last_observed_run_id": R3}


def test_first_seen_of_a_fault_carried_without_one_comes_from_the_saved_scans(tmp_path):
    runs = tmp_path / "runs"
    _save(runs, R1, [_legacy("old")])
    _save(runs, R2, [_legacy("old"), _legacy("newer")])
    _save(runs, R3, [_legacy("old"), _legacy("newer")])
    rows = [{**_legacy("old"), "first_observed_run_id": None}, {**_legacy("newer"), "first_observed_run_id": None},
            {**_legacy("dated"), "first_observed_run_id": R3, "first_observed_exact": True}]

    first_seen_from_saved_scans(rows, runs, before="20261007T000000Z")

    by_name = {r["service"]: (r["first_observed_run_id"], r["first_observed_exact"]) for r in rows}
    # `old` is already open in the oldest saved scan, so it may be older than that.
    assert by_name == {"old": (R1, False), "newer": (R2, True), "dated": (R3, True)}


def test_first_seen_stops_at_a_saved_scan_that_cannot_be_read_and_ignores_later_scans(tmp_path):
    runs = tmp_path / "runs"
    _save(runs, R1, None)                       # saved without a fault list: history ends here
    _save(runs, R2, [_legacy("old")])
    _save(runs, "20261009T000000Z", [])         # later than `before`: not history
    rows = [{**_legacy("old"), "first_observed_run_id": None}]

    first_seen_from_saved_scans(rows, runs, before=R3)

    assert (rows[0]["first_observed_run_id"], rows[0]["first_observed_exact"]) == (R2, False)

    rows = [{**_legacy("old"), "first_observed_run_id": None}]
    first_seen_from_saved_scans(rows, tmp_path / "missing", before=R3)
    assert (rows[0]["first_observed_run_id"], rows[0]["first_observed_exact"]) == (None, False)


def test_saved_scan_that_recorded_first_seen_is_taken_at_its_word(tmp_path):
    runs = tmp_path / "runs"
    _save(runs, R1, [])
    _save(runs, R2, [{**_legacy("old"), "first_observed_run_id": R1, "first_observed_exact": False}])
    rows = [{**_legacy("old"), "first_observed_run_id": None}]

    first_seen_from_saved_scans(rows, runs, before=R3)

    assert (rows[0]["first_observed_run_id"], rows[0]["first_observed_exact"]) == (R1, False)


# --- age of a down link, from the device's own clock -----------------------------

def _interface(name: str, tail: str) -> str:
    return f"""

Sat Oct 10 19:59:06.712 UTC
{name} is down, line protocol is down
  Interface state transitions: {'0' if 'never' in tail else '118'}
  Hardware is HundredGigE, address is 7cf8.804e.8a64 (bia 7cf8.804e.8a64)
  loopback not set,
{tail}
  Last clearing of "show interface" counters never
     0 input errors, 0 CRC, 0 frame, 0 overrun, 0 ignored, 0 abort
RP/0/RP0/CPU0:mich-data-sw#"""


FLAPPED = "  Last link flapped 8w0d\n  Last input 8w0d, output 8w0d"
NEVER = "  Last input never, output never"


def test_link_age_is_read_from_the_investigations_own_interface_output():
    links = [f"{PORT}/0", f"{PORT}/2", f"{PORT}/3", f"{PORT}/1"]
    readings = [
        (f"interface {PORT}/0", _interface(f"{PORT}/0", FLAPPED)),
        (f"interface {PORT}/2", _interface(f"{PORT}/2", FLAPPED.replace("8w0d", "19w4d"))),
        (f"interface {PORT}/3", _interface(f"{PORT}/3", NEVER)),
        ("controllers optics 0/0/0/24", "Rx Power -40.0 dBm"),
    ]

    assert pi._link_ages(links, readings) == {f"{PORT}/0": "8w0d", f"{PORT}/2": "19w4d", f"{PORT}/3": "never"}
    # An up link has no down time, and two interfaces in one answer are not told apart.
    up = _interface(f"{PORT}/0", FLAPPED).replace("is down, line protocol is down", "is up, line protocol is up")
    assert pi._link_ages(links, [("interface x", up)]) == {}
    both = _interface(f"{PORT}/0", FLAPPED) + _interface(f"{PORT}/2", FLAPPED)
    assert pi._link_ages(links, [("interfaces", both)]) == {}


def test_device_uptime_text_converts_to_days():
    assert [age_days(t) for t in ("8w0d", "19w4d", "1y12w", "3d04h", "04:12:33", "never", "", "soon")] == [
        56, 137, 449, 3, 0, None, None, None]
    assert run_day("20261005T191030Z") == "5 Oct" and run_day("r1") == "`r1`"


# --- the open faults, grouped by what explains them -------------------------------

def _row(name: str, *, source: str = "port_investigation", first: str | None = R1, exact: bool = False,
         rechecked: bool = True, kind: str = "l2bridge", status: str = "down") -> dict:
    return {"service": name, "status": status, "service_type": kind, "last_observed_run_id": R3, "source": source,
            "first_observed_run_id": first, "first_observed_exact": exact, "rechecked": rechecked}


def _open_case() -> tuple[CaseFile, list[dict]]:
    case = CaseFile(budget=Budget(0, 0))
    add_evidence(case, {"kind": pi.SOURCE, "role": "port", "layer": "physical", "payload": {
        "device": MICH, "port": PORT, "concluded": True,
        "links": {f"{PORT}/{n}": [f"svc{n}"] for n in range(4)},
        "link_states": {f"{PORT}/{n}": "down" for n in range(4)},
        "link_ages": {f"{PORT}/0": "8w0d", f"{PORT}/1": "8w0d", f"{PORT}/2": "19w4d", f"{PORT}/3": "never"}}})
    add_evidence(case, {"kind": pi.DEVICE_SOURCE, "role": "port", "layer": "routing", "payload": {
        "device": "rutg-data-sw", "concluded": True, "sessions_now": "down", "services": ["cut1", "cut2"]}})
    rows = [_row(f"svc{n}") for n in range(4)]
    rows[3]["first_observed_run_id"] = R2                      # the group shows its oldest
    rows += [_row("cut1", source="device_investigation", first=R3, exact=True, kind="l2sts"),
             _row("cut2", source="device_investigation", first=R3, exact=True, kind="l3rt"),
             _row("alone", source="llm", first=R2, exact=True, kind="l3rt"),
             _row("stale", source="llm", first=None, rechecked=False, kind="l2sts", status="degraded")]
    return case, rows


def test_open_faults_are_grouped_by_the_port_or_device_that_explains_them():
    case, rows = _open_case()

    lines = format_open_faults(open_fault_groups(case, rows))

    assert lines == [
        f"- `{MICH}` port {PORT}: 4 l2bridge Down. First seen 5 Oct or earlier. "
        "Links down 8 to 19 weeks; 1 never came up.",
        "- `rutg-data-sw` cut off: 2 services Down. First seen 6 Oct.",
        "- `l3rt/alone` Down. First seen 6 Oct.",
        "- `l2sts/stale` Degraded, not rechecked this scan.",
    ]


def test_port_group_holds_only_the_services_still_open_and_says_when_no_link_ever_came_up():
    case, rows = _open_case()
    finding = pi.port_findings(case)[0]
    finding["link_ages"] = {link: "never" for link in finding["links"]}

    lines = format_open_faults(open_fault_groups(case, rows[:2]))

    assert lines == [f"- `{MICH}` port {PORT}: 2 l2bridge Down. First seen 5 Oct or earlier. 2 links never came up."]

    finding["link_ages"] = {f"{PORT}/0": "3d04h"}
    assert format_open_faults(open_fault_groups(case, rows[:1]))[0].endswith("Link down 3 days.")
    finding.pop("link_ages")
    assert format_open_faults(open_fault_groups(case, rows[:1]))[0].endswith("First seen 5 Oct or earlier.")


def test_fault_found_this_scan_is_reported_as_new_and_not_also_as_still_open():
    case, rows = _open_case()

    groups = open_fault_groups(case, rows, new={"svc0", "alone"})

    assert [g["services"] for g in groups] == [["svc1", "svc2", "svc3"], ["cut1", "cut2"], ["stale"]]
    assert digest_changes({}, R2, groups)[-4] == "**Still open:** 6 services, 3 causes"
    # With nothing to compare with, every fault is listed and none is called still open.
    assert digest_changes(None, None, open_fault_groups(case, rows))[2] == "**Open service faults:** 8 services, 4 causes"


ALSO = "Not operational (source interface is also a destination)."
SAME = "Not operational (source same as destination)."


def _decided(name: str, device: str, reason: str) -> dict:
    """A port-mirror the fixed check found Down on the device's own word."""
    return {"name": name, "service_type": "port-mirror", "devices": [device], "status": "down",
            "basic_checks": {"status": "down", "needs_investigation": False, "reason": reason, "checks": [
                {"check": "session", "status": "pass", "device": device, "observation": f"mon_{name}"},
                {"check": "source", "status": "fault", "device": device, "observation": reason, "conclusive": True}]}}


def _decided_case() -> tuple[CaseFile, list[dict]]:
    """Three mirrors with one reason on two devices, one with another reason: as on 13 October 2026."""
    records = [_decided("a1", "amst-data-sw", f"TF0/0/0/10/0: {ALSO}"), _decided("a2", "amst-data-sw", f"TF0/0/0/10/1: {ALSO}"),
               _decided("c1", "cern-data-sw", f"TF0/0/0/22/1: {ALSO}"), _decided("c2", "cern-data-sw", f"TF0/0/0/22/0: {SAME}")]
    case = CaseFile(budget=Budget(0, 0))
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services", "payload": {"extra": {
        "services": {f"port-mirror/{r['name']}": r for r in records}}}})
    return case, [_row(r["name"], source="collection", first=R3, exact=True, kind="port-mirror") for r in records]


def test_faults_the_fixed_check_decided_are_grouped_by_the_reason_the_device_gave():
    case, rows = _decided_case()

    lines = format_open_faults(open_fault_groups(case, rows))

    assert lines == [
        '- "Not operational (source interface is also a destination)": 3 port-mirror Down. First seen 6 Oct.',
        "- `port-mirror/c2` Down. First seen 6 Oct.",
    ]
    assert digest_changes({}, R2, open_fault_groups(case, rows))[-3] == "**Still open:** 4 services, 2 causes"


def test_fault_that_still_needs_an_investigation_is_not_put_in_a_reason_group():
    case, rows = _decided_case()
    services = case.evidence[0]["payload"]["extra"]["services"]
    services["port-mirror/a1"]["basic_checks"]["needs_investigation"] = True      # its source port is down too

    lines = format_open_faults(open_fault_groups(case, rows))

    assert lines[0] == '- "Not operational (source interface is also a destination)": 2 port-mirror Down. First seen 6 Oct.'
    assert "- `port-mirror/a1` Down. First seen 6 Oct." in lines


def test_follow_up_gives_decided_faults_one_item_per_reason_and_asks_for_no_investigation():
    from diagnostic_mas.case import open_issue
    from diagnostic_mas.operator_report import format_followup_operator

    case, _rows = _decided_case()
    for name in ("a1", "a2", "c1", "c2"):
        open_issue(case, code="service_down", severity="high", layer="services", edge_id=name, devices=[],
                   message=f"port-mirror {name}: down", evidence_ids=[])

    items = [line.split(". ", 1)[1] for line in format_followup_operator(case)]      # without the item numbers

    closing = "no investigation is needed; resolving it is a change for a person to decide and approve."
    assert items[:2] == [
        "3 port-mirror services are Down on the fixed check's own reading, \"Not operational (source interface is "
        f"also a destination)\", on `amst-data-sw` and `cern-data-sw`: {closing} Services: `a1`, `a2`, `c1`.",
        "`c2` is Down on the fixed check's own reading, \"TF0/0/0/22/0: Not operational (source same as "
        f"destination)\": {closing}",
    ]
    assert "Investigate collection-reported fault" not in "\n".join(items)


def test_follow_up_names_a_few_services_of_a_large_group_and_counts_the_rest():
    from diagnostic_mas.case import open_issue
    from diagnostic_mas.operator_report import format_followup_operator

    records = [_decided(f"m{n:02d}", f"pe{n % 5}-data-sw", f"TF0/0/0/10/{n}: {ALSO}") for n in range(24)]
    case = CaseFile(budget=Budget(0, 0))
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services", "payload": {"extra": {
        "services": {f"port-mirror/{r['name']}": r for r in records}}}})
    for r in records:
        open_issue(case, code="service_down", severity="high", layer="services", edge_id=r["name"], devices=[],
                   message="down", evidence_ids=[])

    item = format_followup_operator(case)[0].split(". ", 1)[1]

    assert item.startswith("24 port-mirror services are Down on the fixed check's own reading, ")
    assert ", on 5 devices: " in item
    assert item.endswith("Services: `m00`, `m01`, `m02`, `m03`, `m04`, `m05`, +18 more (see Services).")


def test_long_list_of_single_faults_is_capped_with_a_count_of_the_rest():
    rows = [_row(f"s{n}", source="llm", exact=True) for n in range(8)]

    lines = format_open_faults(open_fault_groups(CaseFile(budget=Budget(0, 0)), rows), limit=5)

    assert len(lines) == 6 and lines[-1] == "- …and 3 more"


# --- the lines that lead the notification -------------------------------------------

def test_digest_leads_with_changes_and_the_age_of_what_is_still_open():
    case, rows = _open_case()
    delta = {
        "newly_failed_services": [
            {"service": "fresh", "status": "down", "service_type": "l2sts", "detail": "x"},
            {"service": "fabnet1-02c8e17e-a71d-4f22-8165-486946e58786", "status": "down", "service_type": "l3rt"}],
        "service_faults_cleared": [{"service": "fixed", "service_type": "l3rt", "was": "down",
                                    "last_observed_run_id": R2}],
        "recovered_devices": ["star-data-sw"],
        "interfaces_lost": [{"device": "fiu-data-sw", "interface": "HundredGigE0/0/0/25/0", "now": "down",
                             "sub_interfaces": 1}],
        "routing_sessions_lost": [],
        "newly_missing_evidence": [{"subject": f"svc-{n}", "kind": "gap"} for n in range(72)],
    }
    rows.append(_row("n3s-8476cfbe-5677-4f8f-ba9e-5bffb329d412", source="llm", exact=True))

    lines = digest_changes(delta, R2, open_fault_groups(case, rows))

    assert lines[:7] == [
        "**Since the previous scan** (`20261006T000518Z`):",
        "- New service faults: 2 — `l2sts/fresh` down, `l3rt/fabnet1-02c8e17e…` down",
        "- Cleared: 1 — `l3rt/fixed`",
        "- Devices answering again: `star-data-sw`",
        "- Newly missing evidence: 72 (listed in the full report)",
        "- Links up before, down now: 1 — `fiu-data-sw`: HundredGigE0/0/0/25/0 (+1 sub-interface)",
        "",
    ]
    assert lines[7] == "**Still open:** 9 services, 5 causes"
    assert lines[8].startswith(f"- `{MICH}` port {PORT}: 4 l2bridge Down.")
    # Long service ids are cut to their first block here; the full report keeps them whole.
    assert lines[-1] == "- `l2bridge/n3s-8476cfbe…` Down. First seen 5 Oct."
    assert format_open_faults(open_fault_groups(case, rows))[-1] == (
        "- `l2bridge/n3s-8476cfbe-5677-4f8f-ba9e-5bffb329d412` Down. First seen 5 Oct.")


def test_digest_says_none_plainly_and_handles_a_first_scan():
    quiet = {"newly_failed_services": [], "service_faults_cleared": [], "recovered_devices": [],
             "interfaces_lost": [], "routing_sessions_lost": [], "newly_missing_evidence": []}

    assert digest_changes(quiet, R2, []) == [
        "**Since the previous scan** (`20261006T000518Z`):",
        "- New service faults: none",
        "- Cleared: none",
        "- Links up before, down now: none",
        "",
        "**Still open:** none",
    ]
    assert digest_changes(None, None, []) == ["No previous scan to compare with.", "", "**Open service faults:** none"]


REPORT = """# NSO Diagnostic Report
**Scope:** 34 devices · 1861 services
**Duration:** 41 min 1 sec
**Result:** Final service status: 1853 OpUp, 8 Down, 0 Degraded, 0 Unknown. 1 device hit automated check_isis_adjacencies timeout: `scm-data-sw` (0 unknown services). 13 inventory mapping observations need clarification.

## Recommended follow-up

1. Prioritize dataplane down on 8 l2bridge services.
"""


def test_notification_puts_the_change_lines_right_after_a_short_result():
    changes = ["**Since the previous scan** (`r1`):", "- New service faults: none"]

    digest = notification_digest(REPORT, "run", changes=changes).splitlines()

    assert digest[3] == "**Result:** 1853 OpUp, 8 Down, 0 Degraded, 0 Unknown."
    assert digest[4:7] == ["", *changes]
    assert digest.index("**Priority follow-up:**") > digest.index(changes[1])
    # Without change lines the notification is as before, apart from the short result.
    plain = notification_digest(REPORT, "run").splitlines()
    assert plain[3] == digest[3] and plain[5] == "**Priority follow-up:**"
    # The status counts lead even when the report also has its long investigation-coverage sentence.
    coverage = "Only 4 services received additional dataplane investigation: 3 passed; 1 inconclusive."
    with_coverage = notification_digest(REPORT + "\n" + coverage + "\n", "run", changes=changes).splitlines()
    assert with_coverage[3] == digest[3] and coverage not in with_coverage
    # A result with no status counts is shown as it is.
    other = notification_digest(REPORT.replace("Final service status: 1853 OpUp", "1853 OpUp"), "run").splitlines()
    assert other[3].startswith("**Result:** 1853 OpUp, 8 Down, 0 Degraded, 0 Unknown. 1 device hit")


def test_report_changes_section_lists_the_open_faults_with_their_age():
    older, newer = _faulted("bad"), _faulted("bad")
    delta = compute_case_delta(older, newer)
    delta["last_known_service_faults"] = service_fault_history(older, newer, previous_run_id=R1, run_id=R2)
    delta["open_faults"] = open_fault_groups(CaseFile(budget=Budget(0, 0)), delta["last_known_service_faults"])

    text = "\n".join(format_changes_since_previous(delta, previous_run_id=R1))

    assert "**Service faults still open:**\n- `l2bridge/bad` Down. First seen 5 Oct.\n" in text
    assert text.index("**Newly detected service faults:**") < text.index("**Service faults still open:**")


async def test_runner_sends_the_change_lines_in_the_notification(tmp_path):
    import argparse
    from unittest.mock import AsyncMock, MagicMock, patch

    from dataclasses import replace

    from diagnostic_mas import run as run_mod
    from test_diagnostic_mas_publish import _settings

    args = argparse.Namespace(
        skip_llm=True, dry_run=False, publish=True, isis_only=False, bgp_only=False, device_only=False,
        service_only=False, skip_service=True, devices="", service_type="", service_id="",
        max_deep_checks=0, max_handoffs=0)

    class _Session:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            return False

    async def scan() -> str:
        # The change lines travel in the digest, which NOTIFICATION_DIGEST switches on.
        settings = replace(_settings(state_dir=tmp_path, dry_run=True), notification_digest=True)
        with patch.object(run_mod, "load_settings", return_value=settings), \
             patch.object(run_mod, "prepare_fabric_llm", return_value=(None, True)), \
             patch.object(run_mod, "mcp_session", return_value=_Session()), \
             patch.object(run_mod, "call_mcp", new=AsyncMock(return_value={"status": "success", "data": {"device": []}})), \
             patch.object(run_mod, "parse_device_names", return_value=["renc-data-sw"]), \
             patch.object(run_mod, "run_mandatory_spines", new=AsyncMock()), \
             patch.object(run_mod, "summary_narrative", new=AsyncMock(return_value="summary")), \
             patch.object(run_mod, "publish_all", return_value=["slack"]) as publish:
            assert await run_mod._run(args) == 0
        return publish.call_args.args[0]

    first = await scan()
    assert "No previous scan to compare with." in first and "**Open service faults:** none" in first

    second = await scan()
    assert "**Since the previous scan** (`" in second and "- New service faults: none" in second
    assert second.index("**Since the previous scan**") < second.index("Full findings and evidence")
