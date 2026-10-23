"""Tests for per-device interface and hardware details in the HTML report."""

from __future__ import annotations

import pytest

from diagnostic_mas.case import Budget, CaseFile, add_evidence
from diagnostic_mas.html_report import render_html_report
from diagnostic_mas.report import render_report
from diagnostic_mas.state_paths import case_to_dict


def _iface(device: str, name: str, **state) -> dict:
    edge = {
        "id": f"if:{device}:{name}",
        "type": "interface",
        "local": {"device": device, "interface": name},
        "remote": None,
    }
    if state:
        edge["state"] = state
    return edge


def _case() -> CaseFile:
    """pe1 answered live queries and has one up/down interface; pe2 did not answer."""
    case = CaseFile(
        budget=Budget(max_deep_checks=0, max_handoffs=0),
        device_names=["pe1", "pe2"],
    )
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "physical",
            "layer": "physical",
            "payload": {
                "static_edges": [
                    _iface("pe1", "HundredGigE0/0/0/1"),
                    _iface("pe1", "HundredGigE0/0/0/2"),
                    _iface("pe2", "HundredGigE0/0/0/1"),
                ],
                "operational_edges": [
                    _iface("pe1", "HundredGigE0/0/0/1", status="up", admin="up", oper="up"),
                    _iface("pe1", "HundredGigE0/0/0/2", status="down", admin="up", oper="down"),
                    _iface("pe2", "HundredGigE0/0/0/1", status="unknown"),
                ],
            },
        },
    )
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "extra": {"hardware_health": {"pe1": {"control_plane": [{"dropped": 5}]}}}
            },
        },
    )
    case.live_verified_devices = ["pe1"]
    return case


def _details(device: str, *, live_verified=("pe1",)) -> str:
    from diagnostic_mas.device_health import device_detail_lines

    verified = None if live_verified is None else list(live_verified)
    return "\n".join(device_detail_lines(_case(), live_verified=verified)[device])


def test_device_details_list_interfaces_hardware_and_exceptions():
    text = _details("pe1")

    assert "Interfaces" in text and "Total:       2" in text
    assert "Interface up/down:" in text and "HundredGigE0/0/0/2" in text
    assert "Control Plane" in text and "5 packets" in text


def test_device_details_leave_out_what_the_report_already_shows():
    text = _details("pe1")

    for heading in ("Status", "Routing", "Routes", "Services", "Action required"):
        assert heading not in text


def test_device_details_say_not_collected_for_a_device_without_live_data():
    text = _details("pe2")

    assert "Interface state was not collected for this device in this run." in text
    assert "not on box" not in text


def test_device_details_keep_mismatches_when_live_coverage_is_not_recorded():
    text = _details("pe2", live_verified=None)

    assert "Inventory mismatch:" in text


def test_html_report_folds_details_into_each_device():
    case = _case()

    doc = render_html_report(render_report(case), "run", case=case_to_dict(case))

    opener = '<details class="device-detail"><summary>Interfaces and hardware</summary><pre>'
    assert doc.count(opener) == 2
    assert "HundredGigE0/0/0/2" in doc
    assert "Interface state was not collected for this device in this run." in doc


def test_text_report_does_not_carry_device_details():
    text = render_report(_case())

    assert "Interface up/down:" not in text
    assert "Appendix" not in text


def test_health_line_points_to_the_html_report_for_details():
    text = render_report(_case())

    assert "--full" not in text
    assert "per-device details are in the HTML report" in text


def test_full_option_is_gone():
    from diagnostic_mas.run import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--full"])


def test_device_with_no_interface_or_hardware_data_gets_no_details():
    from diagnostic_mas.device_health import device_detail_lines

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0), device_names=["pe3"])

    assert device_detail_lines(case, live_verified=["pe3"]) == {}


def _record(doc: str, device: str) -> str:
    """One device's HTML block: from its summary to the next device block."""
    start = doc.index(f"<summary>{device}")
    end = doc.find('<details class="record"', start)
    return doc[start:end if end > 0 else len(doc)]


def test_a_routing_finding_is_given_in_full_in_both_routers_html_blocks():
    from diagnostic_mas.case import open_issue

    salt, seat, star = "salt-data-sw", "seat-data-sw", "star-data-sw"
    edge = f"isis:{salt}:?:{seat}:HundredGigE0/0/0/23.3000"
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0), device_names=[salt, seat, star])
    open_issue(case, code="unidirectional_adjacency", severity="high", layer="underlay", evidence_ids=[],
               edge_id=edge, devices=[salt, seat], message=f"IS-IS adjacency down: {salt} does not list it; "
                                                           f"{seat} HundredGigE0/0/0/23.3000 lists it as Init")
    add_evidence(case, {"kind": "drill_finding", "role": "drill", "payload": {
        "issue_edge_id": edge, "confidence": "medium",
        "observed": "salt lldp neighbors shows NO neighbour on Hu0/0/0/21,\nwhile seat lists salt Port ID HundredGigE0/0/0/21.",
        "cause": "Physical-path asymmetry seat->salt, **not** configuration. " + "Long evidence sentence. " * 40,
        "fix_suggestion": "1. Trace and re-seat the seat Hu0/0/0/23 -> salt Hu0/0/0/21 fibre pair."}})
    add_evidence(case, {"kind": "drill_finding", "role": "drill", "payload": {
        "issue_edge_id": f"if:{star}:HundredGigE0/0/0/64", "cause": "No light on lanes 1 and 2."}})
    text = render_report(case)

    doc = render_html_report(text, "run", case=case_to_dict(case))

    summary = f"<summary>Full finding: IS-IS adjacency {salt} ↔ {seat}</summary>"
    for device in (salt, seat):
        block = _record(doc, device)
        assert summary in block and block.index("Investigated by LLM") < block.index(summary)
        assert "salt lldp neighbors shows NO neighbour on Hu0/0/0/21,\nwhile seat lists salt" in block
        assert "not verified" in block and "1. Trace and re-seat the seat Hu0/0/0/23" in block
        assert ("Long evidence sentence. " * 40).strip() in block          # uncut
        assert "**not**" not in block and "Physical-path asymmetry seat-&gt;salt, not configuration." in block
    assert "Full finding" not in _record(doc, star)                     # a port finding has its own place
    assert "Full finding" not in text                                    # the text report keeps the one-line version
