"""faults.json: each saved scan's faults as new, still open, recovered or not rechecked, for a script to read."""
import argparse
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from diagnostic_mas import run as run_mod
from diagnostic_mas.case import Budget, CaseFile
from diagnostic_mas.scan_faults import FORMAT_VERSION, scan_faults
from diagnostic_mas.state_paths import persist_case
from nso_facts.topology.graph import bgp_edge_id, isis_edge_id

ISIS = isis_edge_id("salt-data-sw", "HundredGigE0/0/0/21", "seat-data-sw", "HundredGigE0/0/0/23.3000")
BGP = bgp_edge_id("wash-data-sw", "10.133.0.9", "cien-data-sw", "10.148.0.1")
OUTSIDE = "bgp-outside:star-data-sw:192.0.2.9"


def _service(name, status, service_type="l2sts"):
    return {"name": name, "service_type": service_type, "devices": ["a"], "device_sync": {"a": "in-sync"},
            "basic_checks": {"status": status, "sync_ready": True, "needs_investigation": False}}


def _routing_issue(edge_id, code):
    return {"id": f"i-{edge_id}", "code": code, "message": "", "evidence_ids": [], "severity": "high",
            "layer": "routing", "edge_id": edge_id, "status": "open"}


def _history(recent, first="20261027T000000Z", exact=True):
    seen = recent.count("S")
    return {"recent": recent, "seen": seen, "read": seen + recent.count("C"),
            "first_seen_run_id": first, "first_seen_exact": exact}


def _case(services=(), issues=(), history=None, faults=()):
    case = CaseFile(budget=Budget(0, 0))
    case.evidence = [{"kind": "spine", "role": "service", "payload": {"extra": {"services": {
        f"{s['service_type']}/{s['name']}": s for s in services}}}}]
    case.issues = list(issues)
    case.item_history = dict(history or {})
    case.last_known_service_faults = list(faults)
    return case


def _by_key(rows):
    return {row["key"]: row for row in rows}


def test_open_services_are_new_or_still_open_and_unknown_ones_are_only_counted():
    """A new fault was not open last scan: it may have been up, Unknown or not there yet, so "not open"."""
    case = _case(
        services=[_service("s-new", "down"), _service("s-old", "degraded"),
                  _service("s-unknown", "unknown"), _service("s-up", "up")],
        faults=[{"service": "s-old", "status": "degraded", "service_type": "l2sts", "rechecked": True,
                 "first_observed_run_id": "20261027T000000Z", "first_observed_exact": False}])
    delta = {"newly_failed_services": [{"service": "s-new", "status": "down"}]}

    out = scan_faults(case, delta, run_id="R2", previous_run_id="R1")

    assert out["new"] == [{"key": "service:s-new", "kind": "service", "service": "s-new", "service_type": "l2sts",
                           "status": "down", "previous_scan": "not open",
                           "first_seen_run_id": "R2", "first_seen_exact": True}]
    assert out["still_open"] == [{"key": "service:s-old", "kind": "service", "service": "s-old",
                                  "service_type": "l2sts", "status": "degraded", "previous_scan": "open",
                                  "first_seen_run_id": "20261027T000000Z", "first_seen_exact": False}]
    assert out["counts"] == {"services": {"up": 1, "down": 1, "degraded": 1, "unknown": 1}}
    assert "service:s-unknown" not in json.dumps(out)


def test_routing_faults_name_their_ends_and_say_what_the_previous_scan_saw():
    case = _case(
        issues=[_routing_issue(ISIS, "configured_no_adjacency"), _routing_issue(BGP, "configured_no_session"),
                _routing_issue(OUTSIDE, "outside_bgp_session_not_established")],
        history={f"routing|{ISIS}": _history("SS", exact=False), f"routing|{BGP}": _history("CS"),
                 f"routing|{OUTSIDE}": _history("-S")})

    out = scan_faults(case, {}, run_id="R2", previous_run_id="R1")

    [new] = out["new"]
    assert new == {"key": BGP, "kind": "bgp", "status": "not established", "code": "configured_no_session",
                   "ends": [{"device": "wash-data-sw", "address": "10.133.0.9"},
                            {"device": "cien-data-sw", "address": "10.148.0.1"}],
                   "previous_scan": "not open", "first_seen_run_id": "R2", "first_seen_exact": True}
    still = _by_key(out["still_open"])
    assert still[ISIS]["kind"] == "isis" and still[ISIS]["status"] == "down"
    assert still[ISIS]["ends"] == [{"device": "salt-data-sw", "interface": "HundredGigE0/0/0/21"},
                                   {"device": "seat-data-sw", "interface": "HundredGigE0/0/0/23.3000"}]
    assert (still[ISIS]["previous_scan"], still[ISIS]["first_seen_run_id"], still[ISIS]["first_seen_exact"]) == (
        "open", "20261027T000000Z", False)
    assert still[OUTSIDE]["kind"] == "outside_bgp"
    assert still[OUTSIDE]["ends"] == [{"device": "star-data-sw", "address": "192.0.2.9"}]
    assert (still[OUTSIDE]["previous_scan"], still[OUTSIDE]["first_seen_run_id"]) == ("not read", None)


def test_only_a_fault_read_clear_after_being_open_is_recovered():
    other = isis_edge_id("amst-data-sw", "Hu0/0/0/1", "cern-data-sw", "Hu0/0/0/2")
    unread = isis_edge_id("brist-data-sw", "Hu0/0/0/3", "newy-data-sw", "Hu0/0/0/4")
    long_ago = isis_edge_id("kans-data-sw", "Hu0/0/0/5", "dall-data-sw", "Hu0/0/0/6")
    case = _case(
        history={f"routing|{ISIS}": _history("SC"), f"routing|{BGP}": _history("S-C"),
                 f"routing|{unread}": _history("S-"), f"routing|{long_ago}": _history("SCC"),
                 f"routing|{other}": _history("SS-"), "link|salt-data-sw:Hu0/0/0/21": _history("SC")},
        faults=[{"service": "s-stale", "status": "down", "service_type": "l3rt", "rechecked": False,
                 "first_observed_run_id": "20261101T000000Z", "first_observed_exact": True,
                 "last_observed_run_id": "R1"}])
    delta = {"service_faults_cleared": [{"service": "s-fixed", "service_type": "l2sts", "was": "down",
                                         "last_observed_run_id": "R1"}]}

    out = scan_faults(case, delta, run_id="R2", previous_run_id="R1")

    recovered = _by_key(out["recovered"])
    assert set(recovered) == {ISIS, BGP, "service:s-fixed"}
    assert recovered["service:s-fixed"] == {"key": "service:s-fixed", "kind": "service", "service": "s-fixed",
                                            "service_type": "l2sts", "was": "down", "last_seen_run_id": "R1"}
    assert recovered[ISIS]["kind"] == "isis" and recovered[ISIS]["ends"][0]["device"] == "salt-data-sw"
    assert set(_by_key(out["not_rechecked"])) == {unread, other, "service:s-stale"}
    assert _by_key(out["not_rechecked"])["service:s-stale"]["last_seen_run_id"] == "R1"
    assert out["new"] == [] and out["still_open"] == []


def test_without_a_previous_scan_every_open_fault_is_new():
    case = _case(services=[_service("s-down", "down")], issues=[_routing_issue(ISIS, "configured_no_adjacency")],
                 history={f"routing|{ISIS}": _history("S")})

    out = scan_faults(case, None, run_id="R1", previous_run_id=None)

    assert out["previous_run_id"] is None
    assert {(row["key"], row["previous_scan"], row["first_seen_exact"]) for row in out["new"]} == {
        ("service:s-down", "none", False), (ISIS, "none", False)}
    assert out["still_open"] == out["recovered"] == out["not_rechecked"] == []


def test_the_scan_facts_say_what_was_compared_and_what_was_scanned():
    full = scan_faults(_case(), {}, run_id="R2", previous_run_id="R1", report_url="https://reports.invalid/R2")
    focused = scan_faults(_case(), {}, run_id="R2", previous_run_id="R1", service_type="l2sts")

    assert {k: full[k] for k in ("format_version", "run_id", "previous_run_id", "scope", "report_url")} == {
        "format_version": FORMAT_VERSION, "run_id": "R2", "previous_run_id": "R1",
        "scope": {"kind": "full", "service_type": None, "service_id": None},
        "report_url": "https://reports.invalid/R2"}
    assert focused["scope"] == {"kind": "services", "service_type": "l2sts", "service_id": None}
    assert focused["report_url"] is None
    json.dumps(full)


def test_a_saved_scan_writes_faults_json_and_latest_json_points_at_it(tmp_path):
    state = tmp_path / "diagnostic_mas"

    out = persist_case(state, run_id="R2", case=_case(), report="# r\n", faults={"format_version": 1})

    assert json.loads((out.parent / "faults.json").read_text()) == {"format_version": 1}
    assert json.loads((state / "latest.json").read_text())["faults_path"] == str(out.parent / "faults.json")


def test_a_case_saved_without_faults_writes_no_faults_json(tmp_path):
    out = persist_case(tmp_path, run_id="R1", case=_case())

    assert not (out.parent / "faults.json").exists()
    assert "faults_path" not in json.loads((tmp_path / "latest.json").read_text())


class _Session:
    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *exc):
        return False


async def _scan(*, dry_run):
    args = argparse.Namespace(
        skip_llm=True, dry_run=dry_run, publish=not dry_run, isis_only=False, bgp_only=False, device_only=False,
        service_only=False, skip_service=True, devices="", service_type="", service_id="",
        max_deep_checks=0, max_handoffs=0, skip_metrics=True)
    stubs = {"mcp_session": MagicMock(return_value=_Session()),
             "call_mcp": AsyncMock(return_value={"status": "success", "data": {"device": []}}),
             "parse_device_names": MagicMock(return_value=["pe1"]),
             "run_mandatory_spines": AsyncMock(),
             "render_report": MagicMock(return_value="# report\n"),
             "publish_all": MagicMock(return_value=[])}
    with ExitStack() as stack:
        for name, stub in stubs.items():
            stack.enter_context(patch.object(run_mod, name, new=stub))
        return await run_mod._run(args)


@pytest.mark.asyncio
async def test_a_published_scan_saves_its_faults_json(nso_env, llm_requests, tmp_path):
    assert await _scan(dry_run=False) == 0

    [path] = tmp_path.rglob("faults.json")
    faults = json.loads(path.read_text())
    assert faults["run_id"] == path.parent.name and faults["format_version"] == FORMAT_VERSION


@pytest.mark.asyncio
async def test_a_dry_run_saves_no_faults_json(nso_env, llm_requests, tmp_path):
    assert await _scan(dry_run=True) == 0

    assert list(tmp_path.rglob("faults.json")) == []


def test_an_old_scans_faults_json_goes_with_it(tmp_path):
    from diagnostic_mas.state_paths import prune_saved_runs

    old = tmp_path / "runs" / "20261001T000000Z"
    old.mkdir(parents=True)
    for name in ("case.json", "report.md", "report.html", "mcp_calls.txt", "faults.json"):
        (old / name).write_text("x", encoding="utf-8")

    assert prune_saved_runs(tmp_path, run_id="20261103T041450Z", keep_days=30) == 1
    assert not old.exists()


def test_an_interface_the_scan_could_not_name_is_null_not_a_placeholder():
    from nso_facts.topology.underlay import UNREAD_INTERFACE

    one_sided = isis_edge_id("salt-data-sw", UNREAD_INTERFACE, "seat-data-sw", "HundredGigE0/0/0/23.3000")
    case = _case(issues=[_routing_issue(one_sided, "unidirectional_adjacency")],
                 history={f"routing|{one_sided}": _history("SS")})

    [row] = scan_faults(case, {}, run_id="R2", previous_run_id="R1")["still_open"]

    assert row["key"] == one_sided
    assert row["ends"] == [{"device": "salt-data-sw", "interface": None},
                           {"device": "seat-data-sw", "interface": "HundredGigE0/0/0/23.3000"}]


@pytest.mark.asyncio
async def test_a_faults_json_that_cannot_be_built_does_not_stop_the_scan_saving(
        nso_env, llm_requests, tmp_path, monkeypatch, capsys):
    from diagnostic_mas import scan_faults as module

    def broken(*args, **kwargs):
        raise KeyError("status")

    monkeypatch.setattr(module, "scan_faults", broken)

    assert await _scan(dry_run=False) == 0

    assert list(tmp_path.rglob("report.html")) and not list(tmp_path.rglob("faults.json"))
    assert "faults.json not written (KeyError" in capsys.readouterr().err
