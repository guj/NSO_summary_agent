"""A BGP session the devices report down must be visible outside the service findings."""

from __future__ import annotations

from diagnostic_mas.case import Budget, CaseFile, open_issue
from diagnostic_mas.operator_report import (
    format_devices_operator,
    format_followup_operator,
    format_result_line,
)


def _case(sessions: int = 1) -> CaseFile:
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = ["hub-data-sw"] + [f"pe{n}-data-sw" for n in range(1, sessions + 1)]
    for n in range(1, sessions + 1):
        open_issue(
            case, code="session_down", severity="high", layer="routing", evidence_ids=[],
            edge_id=f"bgp:10.0.0.1:10.0.{n}.1:hub-data-sw:pe{n}-data-sw",
            message=f"BGP session down: hub-data-sw 10.0.0.1 (idle) ↔ pe{n}-data-sw 10.0.{n}.1 (idle)",
        )
    return case


def _device_block(lines: list[str], device: str) -> str:
    text = "\n".join(lines)
    return text.split(f"### {device}\n", 1)[1].split("\n### ", 1)[0]


def test_both_devices_get_an_attention_line_for_a_session_down():
    lines = format_devices_operator(_case())

    for device in ("hub-data-sw", "pe1-data-sw"):
        block = _device_block(lines, device)
        assert "**Attention:**" in block
        assert "BGP session down: hub-data-sw 10.0.0.1 (idle) ↔ pe1-data-sw 10.0.1.1 (idle)" in block


def test_result_line_counts_sessions_down_and_names_the_devices():
    one = format_result_line(_case())
    many = format_result_line(_case(sessions=5))

    assert "1 BGP session down: `hub-data-sw` ↔ `pe1-data-sw`." in one
    assert "5 BGP sessions down: " in many and "and 2 more" in many
    assert "BGP session" not in format_result_line(CaseFile(budget=Budget(0, 0)))


def test_follow_up_names_the_sessions_down():
    items = "\n".join(format_followup_operator(_case(sessions=2)))

    assert "2 BGP sessions down" in items
    assert "`hub-data-sw` ↔ `pe1-data-sw`" in items and "`hub-data-sw` ↔ `pe2-data-sw`" in items


# A concluded LLM drill on an IS-IS adjacency or BGP session: shown with the issue, in both devices' blocks.
SALT, SEAT, STAR, CAPE = "salt-data-sw", "seat-data-sw", "star-data-sw", "cape-data-sw"
ISIS_EDGE = f"isis:{SALT}:?:{SEAT}:HundredGigE0/0/0/23.3000"
BGP_EDGE = f"bgp:10.129.128.1:10.148.128.1:{STAR}:{CAPE}"


def _finding(case: CaseFile, edge: str, cause: str, fix: str, **extra) -> None:
    from diagnostic_mas.case import add_evidence

    add_evidence(case, {"kind": "drill_finding", "role": "drill", "payload": {
        "issue_edge_id": edge, "cause": cause, "fix_suggestion": fix, "confidence": "medium",
        "fix_requires_human_approval": True, **extra}})


def _investigated() -> CaseFile:
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = [CAPE, SALT, SEAT, STAR]
    open_issue(case, code="unidirectional_adjacency", severity="high", layer="underlay", evidence_ids=[],
               edge_id=ISIS_EDGE, devices=[SALT, SEAT],
               message=f"IS-IS adjacency down: {SALT} does not list it (its IS-IS names no interface); "
                       f"{SEAT} HundredGigE0/0/0/23.3000 lists it as Init")
    _finding(case, ISIS_EDGE, "Seat's hellos never reach salt's VLAN 3000 sub-interface.",
             "On seat-data-sw run show isis interface HundredGigE0/0/0/23.3000.")
    open_issue(case, code="configured_no_session", severity="medium", layer="routing", evidence_ids=[],
               edge_id=BGP_EDGE, devices=[STAR, CAPE], message=f"{STAR} has no session to {CAPE}")
    _finding(case, BGP_EDGE, "10.148.128.1 is not in star's routing table.", "Restore the underlay to cape first.",
             session_state="not_established")
    _finding(case, f"if:{STAR}:HundredGigE0/0/0/64", "Lanes 1 and 2 receive no light.", "Clean the fibres.")
    return case


def test_a_routing_drill_finding_is_shown_with_its_issue_in_both_devices_blocks():
    lines = format_devices_operator(_investigated())

    for device in (SALT, SEAT):
        assert ("**Investigated by LLM** (confidence medium; not verified): IS-IS adjacency "
                f"{SALT} ↔ {SEAT} — Seat's hellos never reach salt's VLAN 3000 sub-interface. "
                "Next: On seat-data-sw run show isis interface HundredGigE0/0/0/23.3000.") in _device_block(lines, device)
    for device in (STAR, CAPE):
        block = _device_block(lines, device)
        assert (f"BGP session {STAR} ↔ {CAPE} — 10.148.128.1 is not in star's routing table. "
                "Next: Restore the underlay to cape first.") in block
    assert "Lanes 1 and 2 receive no light" not in "\n".join(lines)    # a port finding has its own line


def test_a_finding_that_a_session_is_established_keeps_its_own_line_only():
    case = _investigated()
    _finding(case, BGP_EDGE, "The session is Established now.", "None.", session_state="established")
    assert "The session is Established now" not in "\n".join(format_devices_operator(case))


def test_the_follow_up_points_to_the_finding():
    items = "\n".join(format_followup_operator(_investigated()))
    assert ("Investigate 1 IS-IS adjacency down: `salt-data-sw` ↔ `seat-data-sw` (each router's view and port state "
            "are in its Attention line; an LLM investigation's finding is in the devices' blocks)") in items


def test_the_bgp_follow_up_points_to_a_finding_on_a_session_down():
    case = _case()
    _finding(case, "bgp:10.0.0.1:10.0.1.1:hub-data-sw:pe1-data-sw", "pe1's loopback is not in hub's table.", "Check pe1.")
    assert ("Investigate 1 BGP session down: `hub-data-sw` ↔ `pe1-data-sw` (reported by the devices; an LLM "
            "investigation's finding is in the devices' blocks)") in "\n".join(format_followup_operator(case))
    assert "LLM investigation" not in "\n".join(format_followup_operator(_case()))
