"""An LLM "Up" needs a fixed-check reading from every device, and a status that moves on unchanged checks is flagged.

The case behind both: services on a device whose show commands the fixed checks
cannot use came out Up in one scan and Unknown in the next on the same readings.
"""
from diagnostic_mas.case_delta import compute_case_delta, format_changes_since_previous
from diagnostic_mas.dataplane_verify import accept_dataplane_conclusion
from diagnostic_mas.open_faults import digest_changes
from test_diagnostic_case_delta import _scan

CIEN, STAR = "cien-data-sw", "star-data-sw"
ASKED = ("interfaces brief", "interfaces Generic14.2008", "l2vpn bridge-domain detail")


def record(devices=(CIEN,), *, answered=()):
    """A service whose fixed check asked every device; only those in ``answered`` gave a usable answer."""
    devices = list(devices)
    sources = [{"device": d, "tool": "exec_show", "command": c, "available": d in answered,
                "collected_at": "2026-10-06T22:30:00+00:00"} for d in devices for c in ASKED]
    return {"name": "net19", "service_type": "l3rt", "devices": devices,
            "basic_checks": {"schema_version": 1, "service_id": "net19", "service_type": "l3rt", "devices": devices,
                             "sync_ready": True, "status": "unknown", "checked_at": "2026-10-06T22:31:00+00:00",
                             "sources": sources,
                             "checks": [{"device": d, "check": "attachment", "status": "pass" if d in answered else "unknown"}
                                        for d in devices]}}


def finding(status="up"):
    return {"dataplane_status": status, "observed": "Dig (NSO live-status): port 14 up, gateway BVI4290 up.",
            "cause": "No fault found: PE-side readiness is established.", "confidence": "medium"}


def live_status(*devices):
    return [{"kind": "drill", "payload": {"check": "get_live_status", "args": {"device_name": d},
                                           "result": "oper-status UP"}} for d in devices]


def test_up_is_refused_when_no_fixed_check_could_read_the_device():
    out = accept_dataplane_conclusion(record(), finding(), session_evidence=live_status(CIEN))

    assert (out["dataplane_status"], out["complete"]) == ("unknown", False)
    assert "the fixed checks could not read cien-data-sw" in out["cause"]
    # What the LLM read is kept for the reader.
    assert "port 14 up, gateway BVI4290 up" in out["observed"] and "PE-side readiness is established" in out["cause"]


def test_up_stands_when_the_fixed_check_read_something_from_every_device():
    out = accept_dataplane_conclusion(record(answered=(CIEN,)), finding(), session_evidence=live_status(CIEN))

    assert out["dataplane_status"] == "up"


def test_one_unread_device_among_several_is_enough_to_refuse_and_only_it_is_named():
    out = accept_dataplane_conclusion(record((CIEN, STAR), answered=(STAR,)), finding(),
                                      session_evidence=live_status(CIEN, STAR))

    assert out["dataplane_status"] == "unknown"
    assert "could not read cien-data-sw;" in out["cause"] and STAR not in out["cause"].split("(LLM")[0]


def test_fault_found_on_an_unread_device_is_still_accepted():
    out = accept_dataplane_conclusion(record(), finding("down"), session_evidence=live_status(CIEN))

    assert out["dataplane_status"] == "down" and "fixed checks could not read" not in out["cause"]


def test_service_whose_fixed_check_recorded_no_reads_is_judged_as_before():
    rec = record()
    rec["basic_checks"]["sources"] = []

    assert accept_dataplane_conclusion(rec, finding(), session_evidence=live_status(CIEN))["dataplane_status"] == "up"


# --- a status that moves while the fixed checks read the same --------------------

GAPS = [{"check": "rib", "status": "unknown", "device": STAR, "observation": "IPv6 2602:fcfb:9:15::/64 in global"}]


def _judged(*, llm_up: bool, checks=GAPS, name="svc") -> dict:
    scan = _scan({name: "l3rt", "steady": "l3rt"})
    services = scan["evidence"][0]["payload"]["extra"]["services"]
    for key in services:
        services[key].update({"in_sync": True, "device_sync": {"pe1": "in-sync"},
                              "basic_checks": {"status": "unknown", "checks": [dict(c) for c in checks]}})
    services["l3rt/steady"]["basic_checks"] = {"status": "up", "checks": []}
    if llm_up:
        scan["diagnoses"] = [{"kind": "dataplane", "source": "llm", "status": "up", "complete": True,
                              "subject": {"name": name, "service_type": "l3rt"}}]
    return scan


def test_status_that_changes_on_identical_fixed_checks_is_listed():
    second = _judged(llm_up=False)
    second["diagnoses"] = [{"kind": "dataplane", "source": "llm", "status": "unknown", "complete": False,
                            "subject": {"name": "svc", "service_type": "l3rt"}}]
    delta = compute_case_delta(_judged(llm_up=True), second)

    assert delta["status_changed_on_unchanged_checks"] == [
        {"service": "svc", "service_type": "l3rt", "was": "up", "now": "unknown"}]
    text = "\n".join(format_changes_since_previous(delta, previous_run_id="r1"))
    assert "**Status changed while the fixed checks read the same:**\n- `l3rt/svc` — OpUp → Unknown\n" in text
    assert "- Status changed on unchanged checks: 1 — `l3rt/svc` OpUp → Unknown" in digest_changes(delta, "r1", [])


def test_scan_that_did_not_investigate_the_service_is_not_a_change_of_verdict():
    """A scan run without the LLM leaves the service Unknown; that is no second opinion."""
    not_investigated = _judged(llm_up=False)
    unresolved = _judged(llm_up=False)
    unresolved["diagnoses"] = [{"kind": "dataplane", "source": "llm", "status": "unknown", "complete": False,
                                "subject": {"name": "svc", "service_type": "l3rt"}}]

    assert compute_case_delta(_judged(llm_up=True), not_investigated)["status_changed_on_unchanged_checks"] == []
    assert compute_case_delta(not_investigated, _judged(llm_up=True))["status_changed_on_unchanged_checks"] == []
    assert compute_case_delta(_judged(llm_up=True), unresolved)["status_changed_on_unchanged_checks"] == [
        {"service": "svc", "service_type": "l3rt", "was": "up", "now": "unknown"}]


def test_nothing_is_listed_when_the_checks_differ_the_status_holds_or_the_service_is_new():
    changed = [dict(GAPS[0], observation="IPv6 2602:fcfb:9:16::/64 in global")]
    assert compute_case_delta(_judged(llm_up=True), _judged(llm_up=False, checks=changed))[
        "status_changed_on_unchanged_checks"] == []
    assert compute_case_delta(_judged(llm_up=True), _judged(llm_up=True))["status_changed_on_unchanged_checks"] == []
    assert compute_case_delta(_judged(llm_up=True, name="other"), _judged(llm_up=False))[
        "status_changed_on_unchanged_checks"] == []
    quiet = compute_case_delta(_judged(llm_up=True), _judged(llm_up=True))
    assert "Status changed" not in "\n".join(format_changes_since_previous(quiet, previous_run_id="r1"))
    assert not any("Status changed" in line for line in digest_changes(quiet, "r1", []))
