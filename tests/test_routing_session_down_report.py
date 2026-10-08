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
