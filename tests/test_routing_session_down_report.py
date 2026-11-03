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


def _read_at_one_end(*sessions: tuple[str, str, str, str], edges: bool = True) -> CaseFile:
    """test30's Idle iBGP sessions: each (local, remote, local state, remote state); cien and cape unreadable."""
    from diagnostic_mas.case import add_evidence

    case = CaseFile(budget=Budget(0, 0))
    static, operational = [], []
    for a, b, state_a, state_b in sessions:
        edge = f"bgp:10.0.0.1:10.0.0.2:{a}:{b}"
        static.append({"id": edge, "local": {"device": a}, "remote": {"device": b}})
        operational.append({"id": edge, "state": {"local": state_a, "remote": state_b, "status": "down"}})
        open_issue(case, code="configured_no_session", severity="high", layer="routing", evidence_ids=[],
                   edge_id=edge, message=f"BGP {a} 10.0.0.1 ({state_a}) ↔ {b} 10.0.0.2 ({state_b}): "
                                         "Observed non-established BGP session; missing endpoint evidence "
                                         "remains unverified")
    if edges:
        add_evidence(case, {"kind": "spine", "role": "bgp", "layer": "routing", "payload": {
            "static_edges": static, "operational_edges": operational}})
    return case


def test_result_line_counts_sessions_read_at_one_end_only_by_the_end_that_was_not_read():
    case = _read_at_one_end((STAR, CAPE, "idle", "unknown"), ("dall-data-sw", "cien-data-sw", "idle", "unknown"),
                            (SALT, "cien-data-sw", "idle", "unknown"), (STAR, "cien-data-sw", "active", "unknown"),
                            ("cien-data-sw", "wash-data-sw", "unknown", "idle"))

    result = format_result_line(case)

    assert ("5 BGP sessions not established, as read at one end only: to `cien-data-sw` from `dall-data-sw`, "
            "`salt-data-sw`, `star-data-sw` and `wash-data-sw`; to `cape-data-sw` from `star-data-sw`.") in result
    assert "BGP session down" not in result and "BGP sessions down" not in result


def test_result_line_names_both_ends_when_it_cannot_tell_which_end_was_read():
    result = format_result_line(_read_at_one_end((STAR, CAPE, "idle", "unknown"), edges=False))

    assert "1 BGP session not established: `star-data-sw` ↔ `cape-data-sw`." in result


def test_follow_up_names_the_sessions_read_at_one_end_only():
    items = "\n".join(format_followup_operator(_read_at_one_end(
        (STAR, CAPE, "idle", "unknown"), ("dall-data-sw", "cien-data-sw", "idle", "unknown"),
        (SALT, "cien-data-sw", "idle", "unknown"))))

    assert ("Investigate 3 BGP sessions not established, as read at one end only: to `cien-data-sw` from "
            "`dall-data-sw` and `salt-data-sw`; to `cape-data-sw` from `star-data-sw`.") in items


def test_the_follow_up_on_sessions_not_established_points_to_their_findings():
    case = _read_at_one_end((STAR, CAPE, "idle", "unknown"), (SALT, "cien-data-sw", "idle", "unknown"))
    _finding(case, f"bgp:10.0.0.1:10.0.0.2:{STAR}:{CAPE}", "Cape's loopback is not in star's table.", "Check cape.",
             session_state="not_established")
    one = "\n".join(format_followup_operator(case))
    _finding(case, f"bgp:10.0.0.1:10.0.0.2:{SALT}:cien-data-sw", "Cien has no LSP.", "Check cien.",
             session_state="not_established")
    two = "\n".join(format_followup_operator(case))

    assert "to `cien-data-sw` from `salt-data-sw` (an LLM investigation's finding is in the devices' blocks)." in one
    assert "(LLM investigations' findings are in the devices' blocks)." in two


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
               message=f"IS-IS adjacency down: {SALT} does not list it (no entry in its IS-IS adjacency table); "
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


def test_an_earlier_scans_finding_is_shown_dated_while_its_issue_waits_for_a_drill():
    case = _investigated()
    case.evidence = [ev for ev in case.evidence if ev["payload"]["issue_edge_id"] != ISIS_EDGE]
    case.previous_findings = {
        ISIS_EDGE: {"cause": "Seat's hellos never reach salt.", "confidence": "medium", "run_id": "20261030T221052Z"},
        BGP_EDGE: {"cause": "An older reading of the session.", "run_id": "20261030T221052Z"},
    }
    lines = format_devices_operator(case)

    for device in (SALT, SEAT):
        assert ("**Investigated by LLM on 30 Oct** (not rechecked this scan; confidence medium; not verified): "
                f"IS-IS adjacency {SALT} ↔ {SEAT} — Seat's hellos never reach salt.") in _device_block(lines, device)
    assert "An older reading of the session" not in "\n".join(lines)     # this scan concluded on it again
    assert "an LLM investigation's finding is in the devices' blocks" in "\n".join(format_followup_operator(case))


def test_a_finding_is_shown_by_its_headline_when_it_has_one():
    case = _investigated()
    long_cause = "Seat's hellos never reach salt's VLAN 3000 sub-interface. " + "More reasoning. " * 40
    _finding(case, ISIS_EDGE, long_cause, "Read the punt counters.", headline="Salt drops seat's hellos.")

    block = _device_block(format_devices_operator(case), SALT)
    assert f"IS-IS adjacency {SALT} ↔ {SEAT} — Salt drops seat's hellos. Next: Read the punt counters." in block


def test_a_finding_whose_headline_is_too_long_is_shown_by_its_cut_cause():
    case = _investigated()
    _finding(case, ISIS_EDGE, "Seat's hellos never reach salt.", "Read the punt counters.", headline="x " * 300)

    block = _device_block(format_devices_operator(case), SALT)
    assert f"IS-IS adjacency {SALT} ↔ {SEAT} — Seat's hellos never reach salt. Next: Read the punt counters." in block
