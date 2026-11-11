"""The Result's "LLM investigations" line: how the scan's LLM work went."""
from __future__ import annotations

from diagnostic_mas.case import Budget, CaseFile, add_evidence
from diagnostic_mas.llm_status import error_phrase, llm_result_text, note_stop, note_summary_failed

CONNECTION = "connection_error"


def _case(*, skipped: str | None = None) -> CaseFile:
    case = CaseFile(budget=Budget(0, 0))
    case.llm_status.update({"skipped": skipped} if skipped else {"summary": "written"})
    return case


def _service(case: CaseFile, name: str, stop_reason: str = "concluded", **metrics) -> None:
    case.diagnoses.append({
        "kind": "dataplane", "source": "llm", "complete": stop_reason.startswith("concluded"),
        "status": "up" if stop_reason == "concluded" else "unknown",
        "subject": {"name": name, "service_type": "l3rt"}, "observed": "o", "cause": "c",
        "investigation": {"stop_reason": stop_reason, **metrics}})


def _port(case: CaseFile, port: str, *, concluded: bool = True, **extra) -> None:
    case.budget.port_investigations_used += 1
    add_evidence(case, {"kind": "port_investigation", "role": "port", "payload": {
        "device": "fiu-data-sw", "port": port, "concluded": concluded, **extra}})


def _routing(case: CaseFile, edge: str, *, why: str | None = None, error: str | None = None) -> None:
    case.budget.drill_issues_used += 1
    if why:
        case.budget.drill_issues_unconcluded += 1
        note_stop(case, "routing", edge, why, error)


def test_a_scan_whose_investigations_and_summary_all_finished_is_complete():
    case = _case()
    _service(case, "net1")
    _service(case, "net2", "concluded_insufficient_evidence")
    _port(case, "HundredGigE0/0/0/24")
    _routing(case, "isis:salt:?:seat:Hu0/0/0/23.3000")

    assert llm_result_text(case) == "Complete — 4 of 4 investigations reached a conclusion; summary written."


def test_a_scan_that_needed_no_investigation_is_complete():
    assert llm_result_text(_case()) == "Complete — no investigation was needed; summary written."


def test_an_investigation_stopped_by_refused_conclusions_makes_the_scan_partial():
    case = _case()
    _service(case, "net1")
    _routing(case, "bgp:a:b:dall:cien", why="refused")
    _routing(case, "bgp:a:b:star:cien")

    assert llm_result_text(case) == ("Partial — 2 of 3 investigations reached a conclusion "
                                     "(1 stopped after refused conclusions); summary written.")


def test_provider_failures_are_named_by_category_and_the_fixed_checks_stand():
    case = _case()
    for n in range(8):
        _service(case, f"net{n}")
    _service(case, "net8", "provider_failure", llm_error=CONNECTION)
    _port(case, "HundredGigE0/0/0/25", concluded=False, stopped="provider_error", stopped_error=CONNECTION)
    note_summary_failed(case, CONNECTION)

    assert llm_result_text(case) == (
        "Partial — 8 of 10 investigations reached a conclusion (2 failed: provider connection error); "
        "no LLM summary (provider connection error). Fixed-check results stand.")


def test_a_scan_where_no_request_succeeded_is_unavailable():
    case = _case()
    _service(case, "net1", "llm_timeout", llm_error="response_read_timeout")
    _routing(case, "bgp:a:b:star:cien", why="provider_error", error="response_read_timeout")
    note_summary_failed(case, "response_read_timeout")

    assert llm_result_text(case) == ("Unavailable — no LLM request succeeded (provider timed out). "
                                     "Fixed-check results stand.")


def test_an_investigation_that_only_ran_out_of_rounds_is_counted_without_a_provider_claim():
    case = _case()
    _service(case, "net1", "round_limit")
    _service(case, "net2")

    assert llm_result_text(case) == ("Partial — 1 of 2 investigations reached a conclusion "
                                     "(1 ended without a conclusion); summary written.")


def test_a_skipped_scan_says_why():
    assert llm_result_text(_case(skipped="the scan ran with --skip-llm")) == "Skipped — the scan ran with --skip-llm."


def test_a_scan_halted_by_the_providers_spend_limit_says_so():
    case = _case()
    for n in range(4):
        _service(case, f"net{n}")
    _service(case, "net4", "provider_failure", llm_error="RateLimitError")
    case.llm_halt_reason = "provider budget_exceeded: Budget has been exceeded! Current cost: 500.1"

    assert llm_result_text(case) == (
        "Halted — the provider's spend limit was reached; 4 of 5 investigations reached a conclusion; "
        "no LLM summary. Fixed-check results stand.")


def test_a_summary_that_was_not_requested_or_came_back_empty_says_so():
    case = _case()
    _service(case, "net1")
    case.llm_status["summary"] = "not_requested"
    assert llm_result_text(case) == ("Complete — 1 of 1 investigation reached a conclusion; "
                                     "LLM summary not requested.")
    case.llm_status["summary"] = "empty"
    assert llm_result_text(case) == ("Partial — 1 of 1 investigation reached a conclusion; "
                                     "no LLM summary (empty reply).")


def test_a_case_saved_before_this_was_recorded_gets_no_line():
    case = CaseFile(budget=Budget(0, 0))
    _service(case, "net1")

    assert llm_result_text(case) is None


def test_a_provider_error_is_named_by_its_kind_never_its_text():
    assert error_phrase("connection_error") == "provider connection error"
    assert error_phrase("connection_timeout") == "provider connection timed out"
    assert error_phrase("response_read_timeout") == "provider timed out"
    assert error_phrase("InternalServerError") == "provider internal server error"
    assert error_phrase("APIStatusError") == "provider status error"
    assert error_phrase("") == "provider error"


def test_the_result_block_carries_the_line_after_the_fault_and_connection_lines():
    from diagnostic_mas.case import open_issue
    from diagnostic_mas.operator_report import format_result_section

    case = _case()
    _service(case, "net1")
    open_issue(case, code="unknown_neighbor_system_id", severity="low", layer="underlay", evidence_ids=[],
               message="star-data-sw Hu0/0/0/21.856: unmapped IS-IS system id 'uky-data-sw'")

    lines = format_result_section(case, None)

    assert lines[-2] == ("- **LLM investigations:** Complete — 1 of 1 investigation reached a conclusion; "
                         "summary written.")
    assert lines[-1].startswith("- **Unmatched neighbors:**")


def test_a_case_without_the_record_has_no_line_in_the_result_block():
    from diagnostic_mas.operator_report import format_result_section

    case = CaseFile(budget=Budget(0, 0))
    _service(case, "net1")

    assert not any("LLM investigations" in line for line in format_result_section(case, None))


def _down_by_fixed_check_with_a_failed_investigation() -> CaseFile:
    from diagnostic_mas.case import service_records
    from diagnostic_mas.dataplane_verify import _incomplete_verify_finding, _record_incomplete_dataplane_verify

    case = _case()
    add_evidence(case, {"kind": "spine", "role": "service", "payload": {"extra": {"services": {
        "l2bridge/link1": {"name": "link1", "service_type": "l2bridge", "devices": ["fiu-data-sw"],
                           "device_sync": {"fiu-data-sw": "in-sync"}, "system_status": "up",
                           "basic_checks": {"status": "down", "sync_ready": True, "needs_investigation": True,
                                            "checks": [{"check": "attachment", "status": "fault",
                                                        "detail": "Hu0/0/0/24/0"}]}}}}}})
    record = next(r for _ev, _k, r in service_records(case.evidence))
    _record_incomplete_dataplane_verify(
        case, record, _incomplete_verify_finding(record, stop_reason="LLM request failed: InternalServerError"),
        evidence_ids=[], source="llm")
    return case


def test_a_service_the_fixed_check_found_down_says_down_when_its_investigation_fails():
    from diagnostic_mas.operator_report import format_services_operator
    from diagnostic_mas.service_final_status import final_service_counts

    case = _down_by_fixed_check_with_a_failed_investigation()
    block = "\n".join(format_services_operator(case, services_detail=True))

    assert final_service_counts(case)["l2bridge"]["down"] == 1
    assert "**Result:** Down by the fixed check — the LLM investigation did not finish" in block
    assert ("**Cause:** Dataplane verification incomplete because the LLM request failed (InternalServerError). "
            "The fixed check found the service Down; that result stands.") in block
    assert "Unknown category" not in block and "dataplane=unknown" not in block
    assert "remains unknown" not in block


def test_the_fallback_summary_says_down_for_that_service_too():
    from diagnostic_mas.roles.summary import narrative_from_dataplane_findings

    text = narrative_from_dataplane_findings(_down_by_fixed_check_with_a_failed_investigation())

    assert "- **l2bridge link1** — Down by the fixed check (LLM investigation did not finish)" in text
    assert "dataplane=unknown" not in text and "remains unknown" not in text


def test_an_unfinished_port_investigation_says_why():
    from diagnostic_mas.operator_report import _port_finding_lines

    case = _case()
    _port(case, "HundredGigE0/0/0/25", concluded=False, links={"HundredGigE0/0/0/25/0": ["link-tofino-s3"]},
          stopped="provider_error", stopped_error=CONNECTION)
    _port(case, "HundredGigE0/0/0/24", concluded=False, stopped="no_conclusion",
          links={"HundredGigE0/0/0/24/0": ["a"], "HundredGigE0/0/0/24/1": ["b"]})

    assert _port_finding_lines(case, "fiu-data-sw") == [
        "**Port HundredGigE0/0/0/25:** investigation did not finish: the LLM request failed "
        "(provider connection error); its 1 service was left to its own investigation",
        "**Port HundredGigE0/0/0/24:** investigation did not finish; "
        "its 2 services were left to their own investigation"]


_REPORT = """# NSO Diagnostic Report

**Scope:** 34 devices · 1759 services
**Duration:** 47 min
**Result:**
- **Services:** 1750 OpUp, 6 Down, 0 Degraded, 3 Unknown (none newly failed).
- **LLM investigations:** {llm}
- **Inventory:** Device inventory/counter observations need classification.
"""


def test_the_slack_and_email_text_carries_the_line_only_when_the_llm_work_was_not_complete():
    from diagnostic_mas.html_report import notification_digest

    partial = ("Partial — 6 of 9 investigations reached a conclusion (3 failed: provider connection error); "
               "no LLM summary (provider connection error). Fixed-check results stand.")
    digest = notification_digest(_REPORT.format(llm=partial), "run").splitlines()
    assert digest[3:5] == ["**Result:** 1750 OpUp, 6 Down, 0 Degraded, 3 Unknown.", f"**LLM investigations:** {partial}"]

    complete = "Complete — 9 of 9 investigations reached a conclusion; summary written."
    assert not any("LLM investigations" in line for line in notification_digest(_REPORT.format(llm=complete), "run").splitlines())
