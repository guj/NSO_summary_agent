"""Counts for the two rules the operators have not decided yet: reporting only, no status changes."""
from diagnostic_mas.case import Budget, CaseFile, add_evidence
from diagnostic_mas.state_paths import case_to_dict
from diagnostic_mas.undecided_rules import digest_undecided_rules, format_undecided_rules, undecided_rule_counts
from test_device_investigation import FAR, HUB, PE, _loop, _pe_case, _run

LOCAL, STAR, WASH = "site-data-sw", "star-data-sw", "wash-data-sw"
EXACT = "IPv6 2602:fcfb:9:15::/64 in global"
ON_BORDERS = [("rib", STAR, EXACT), ("rib", WASH, EXACT)]


def _service(name: str, gaps: list[tuple[str, str, str]], *, kind: str = "l3rt") -> dict:
    """A service whose fixed check is Unknown with the given gaps: (check, device, observation)."""
    devices = [LOCAL, STAR, WASH]
    checks = [{"check": "rib", "status": "pass", "device": LOCAL, "observation": EXACT}]
    checks += [{"check": check, "status": "unknown", "device": device, "observation": seen}
               for check, device, seen in gaps]
    return {"name": name, "service_type": kind, "devices": devices, "in_sync": True, "system_status": "up",
            "device_sync": {d: "in-sync" for d in devices},
            "basic_checks": {"status": "unknown", "needs_investigation": True, "checks": checks,
                             "intent": {"device": LOCAL}}}


def _case(*services: dict, concluded_up: tuple[str, ...] = ()) -> CaseFile:
    case = CaseFile(budget=Budget(0, 0))
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services", "payload": {"extra": {
        "services": {f"{s['service_type']}/{s['name']}": s for s in services}}}})
    for service in services:
        if service["name"] in concluded_up:
            case.diagnoses.append({"kind": "dataplane", "source": "llm", "status": "up", "complete": True,
                                   "subject": {"name": service["name"], "service_type": service["service_type"]}})
    return case


def test_counts_l3rt_that_are_up_only_because_the_llm_accepted_a_covering_route_on_the_borders():
    case = _case(
        _service("covered", ON_BORDERS),
        _service("not-concluded", ON_BORDERS),                                   # the LLM did not say Up
        _service("local-gap", [("rib", LOCAL, EXACT)]),                          # not a border question
        _service("more-gaps", ON_BORDERS + [("fib", STAR, EXACT)]),              # something else is open too
        _service("default-route", [("rib", STAR, "IPv6 ::/0 in global")]),       # not the service's own prefix
        _service("bridged", ON_BORDERS, kind="l2sts"),
        concluded_up=("covered", "local-gap", "more-gaps", "default-route", "bridged"))

    assert undecided_rule_counts(case, answered=set()) == {"covering_route": ["covered"], "cut_off": []}


async def test_counts_what_a_cut_off_device_holds_down_and_the_internal_l3rt_still_up():
    case = _pe_case()
    assert undecided_rule_counts(case, answered={PE, HUB, FAR})["cut_off"] == [
        {"device": PE, "down": 0, "internal_up": ["local-routed"]}]

    await _run(case, _loop([]))        # the device investigation marks the two services that use another device
    # A service on the device that is Down for a reason of its own is not Down under this rule.
    case.diagnoses.append({"kind": "dataplane", "source": "llm", "status": "down", "complete": True,
                           "subject": {"name": "local-unknown", "service_type": "l3rt"}})

    assert undecided_rule_counts(case, answered={PE, HUB, FAR})["cut_off"] == [
        {"device": PE, "down": 2, "internal_up": ["local-routed"]}]
    # A device that did not answer this scan is never called cut off.
    assert undecided_rule_counts(case, answered={HUB, FAR})["cut_off"] == []


COUNTS = {"covering_route": ["net-a", "pub-b", "pub-c"],
          "cut_off": [{"device": "rutg-data-sw", "down": 7, "internal_up": [f"int{n}" for n in range(15)]}]}


def test_report_block_says_what_each_rule_would_change():
    lines = format_undecided_rules(COUNTS)

    assert lines[:4] == ["## Undecided rules", "",
                         "What each open rule would change. No status in this report depends on these counts.", ""]
    assert lines[4] == (
        "- **Border route:** 3 l3rt are Up because the LLM accepted a covering route where the fixed check wants "
        "the service's exact prefix on each border router: `net-a`, `pub-b`, `pub-c`. If the exact prefix is "
        "required they become Unknown; if a covering route is enough they pass without the LLM.")
    assert lines[5] == (
        "- **Cut-off device `rutg-data-sw`:** 7 services Down under the current rule. 15 internal l3rt are Up and "
        "would be Down if an l3rt must reach other sites: `int0`, `int1`, `int2`, `int3`, `int4`, +10 more.")
    one = format_undecided_rules({"covering_route": ["net-a"], "cut_off": [
        {"device": "pe1", "down": 1, "internal_up": ["int0"]}]})
    assert "1 l3rt is Up because" in one[4] and "it becomes Unknown" in one[4] and "it passes without" in one[4]
    assert "1 service Down under the current rule. 1 internal l3rt is Up and would be Down" in one[5]
    assert format_undecided_rules({"covering_route": [], "cut_off": []}) == []
    assert format_undecided_rules({}) == []


def test_notification_shows_a_cut_off_device_always_and_the_border_count_only_when_it_changes():
    cut_off = ("- `rutg-data-sw` cut off: 7 Down under the current rule; 15 internal l3rt are Up and would be Down "
               "if an l3rt must reach other sites.")
    assert digest_undecided_rules(COUNTS, None) == [
        "**Undecided rules:**", cut_off, "- Border route: 3 l3rt Up on a covering route (first count)."]
    assert digest_undecided_rules(COUNTS, {"covering_route": ["x", "y", "z"], "cut_off": []}) == [
        "**Undecided rules:**", cut_off]

    quiet = {"covering_route": ["net-a", "pub-b"], "cut_off": []}
    assert digest_undecided_rules(quiet, {"covering_route": ["a", "b"]}) == []
    assert digest_undecided_rules(quiet, {"covering_route": ["a", "b", "c"]}) == [
        "**Undecided rules:**", "- Border route: 2 l3rt Up on a covering route (was 3)."]
    assert digest_undecided_rules({"covering_route": [], "cut_off": []}, None) == []


def test_counts_are_saved_with_the_scan_and_shown_after_the_changes_section():
    from diagnostic_mas.report import render_report

    case = _case(_service("covered", ON_BORDERS), concluded_up=("covered",))
    case.undecided_rules = undecided_rule_counts(case, answered=set())

    assert case_to_dict(case)["undecided_rules"] == {"covering_route": ["covered"], "cut_off": []}
    report = render_report(case, summary="s", duration_seconds=1, dry_run=True, llm_model="m")
    assert report.index("## Changes since previous run") < report.index("## Undecided rules") < report.index("## Devices")
    assert "- **Border route:** 1 l3rt is Up because" in report

    case.undecided_rules = {"covering_route": [], "cut_off": []}
    assert "Undecided rules" not in render_report(case, summary="s", duration_seconds=1, dry_run=True, llm_model="m")


async def test_runner_saves_the_counts_and_adds_their_lines_to_the_notification(tmp_path):
    import argparse
    import json
    from unittest.mock import AsyncMock, MagicMock, patch

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

    counts = {"covering_route": ["net-a"], "cut_off": []}
    with patch.object(run_mod, "load_settings", return_value=_settings(state_dir=tmp_path, dry_run=True)), \
         patch.object(run_mod, "prepare_fabric_llm", return_value=(None, True)), \
         patch.object(run_mod, "mcp_session", return_value=_Session()), \
         patch.object(run_mod, "call_mcp", new=AsyncMock(return_value={"status": "success", "data": {"device": []}})), \
         patch.object(run_mod, "parse_device_names", return_value=["renc-data-sw"]), \
         patch.object(run_mod, "run_mandatory_spines", new=AsyncMock()), \
         patch.object(run_mod, "summary_narrative", new=AsyncMock(return_value="summary")), \
         patch("diagnostic_mas.undecided_rules.undecided_rule_counts", return_value=counts), \
         patch.object(run_mod, "publish_all", return_value=["slack"]) as publish:
        assert await run_mod._run(args) == 0

    saved = next((tmp_path / "diagnostic_mas" / "runs").iterdir())
    assert json.loads((saved / "case.json").read_text())["undecided_rules"] == counts
    assert "## Undecided rules" in (saved / "report.md").read_text()
    digest = publish.call_args.args[0]
    assert "**Undecided rules:**\n- Border route: 1 l3rt Up on a covering route (first count)." in digest
    assert digest.index("**Open service faults:**") < digest.index("**Undecided rules:**")
