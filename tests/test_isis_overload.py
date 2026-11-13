"""The IS-IS overload bit of each router, from one read of the link-state database per scan."""

from __future__ import annotations

import pytest

from diagnostic_mas.case import Budget, CaseFile
from diagnostic_mas.isis_overload import (
    KIND, isis_overload_leads, overload_reads_line, parse_isis_database, record_isis_overload)
from diagnostic_mas.operator_report import format_followup_operator, format_result_section, format_run_details
from nso_facts.topology.graph import isis_edge_id

A, B, C = "a-data-sw", "b-data-sw", "c-data-sw"
COMMAND = "isis database level 2"


def _database(*, overloaded: tuple[str, ...] = (), reader: str = A) -> str:
    """``show isis database level 2`` as IOS-XR prints it (12 Nov 2026 layout), with the reader's own row marked."""
    rows = []
    for name in (A, B, C):
        bits = "0/0/1" if name in overloaded else "0/0/0"
        if name == reader:
            rows.append(f"{name}.00-00  * 0x000287ff   0xd46e        820  /*            {bits}")
        else:
            rows.append(f"{name}.00-00     0x00029aaa   0xd719        711  /1200         {bits}")
    # A second fragment carries no router bit of its own: it is not read.
    rows.append(f"{B}.00-01     0x0000fe83   0xa244        1152 /1200         0/0/1")
    return f"""

Thu Nov 12 23:41:33.571 UTC

IS-IS fabric-net (Level-2) Link State Database
LSPID                 LSP Seq Num  LSP Checksum  LSP Holdtime/Rcvd  ATT/P/OL
{chr(10).join(rows)}

 Total Level-2 LSP count: 4     Local Level-2 LSP count: 1

RP/0/RP0/CPU0:{reader}#"""


def test_the_overload_bit_is_the_third_field_of_each_routers_first_fragment_and_the_reader_s_own_row_counts():
    parsed = parse_isis_database(_database(overloaded=(C,)))

    assert parsed == {"routers": {A: False, B: False, C: True}, "total": 4}


def test_a_reply_without_the_database_table_is_not_read():
    assert parse_isis_database("% Invalid input detected at '^' marker.") is None
    assert parse_isis_database("") is None


def _case(*pairs: tuple[str, str]) -> CaseFile:
    case = CaseFile(budget=Budget(0, 0))
    edges = [{"id": isis_edge_id(a, "HundredGigE0/0/0/1.3000", b, "HundredGigE0/0/0/2.3000")} for a, b in pairs]
    case.evidence = [{"id": "ev_1", "kind": "spine", "role": "isis", "payload": {"operational_edges": edges}}]
    return case


def _call(outputs: dict, asked: list):
    async def call(client, tool, args):
        assert tool == "exec_show"
        key = (args["device_name"], args["input_command"])
        asked.append(key)
        return {"status": "success", "data": {"result": outputs.get(key, "% Invalid input detected")}}
    return call


def _saved(case: CaseFile) -> dict:
    return [ev["payload"] for ev in case.evidence if ev["kind"] == KIND][0]


@pytest.mark.asyncio
async def test_one_router_is_asked_and_its_table_stands_for_the_fleet():
    case, asked = _case((A, B), (B, C)), []

    await record_isis_overload(None, case, call=_call({(A, COMMAND): _database(overloaded=(C,))}, asked))

    assert asked == [(A, COMMAND)]
    saved = _saved(case)
    assert (saved["read_on"], saved["routers"], saved["total"]) == (A, {A: False, B: False, C: True}, 4)
    assert isis_overload_leads(case) == [C]


@pytest.mark.asyncio
async def test_the_next_router_is_asked_when_one_gives_no_table_and_three_tries_are_the_most():
    case, asked = _case((A, B), (B, C)), []
    await record_isis_overload(None, case, call=_call({(B, COMMAND): _database(reader=B)}, asked))
    assert asked == [(A, COMMAND), (B, COMMAND)] and _saved(case)["read_on"] == B

    nothing, asked = _case((A, B), (B, C), (C, "d-data-sw")), []
    await record_isis_overload(None, nothing, call=_call({}, asked))
    assert len(asked) == 3                                                   # three tries, then no claim
    saved = _saved(nothing)
    assert (saved["read_on"], saved["routers"], saved["tried"]) == (None, {}, [A, B, C])
    assert isis_overload_leads(nothing) == []


@pytest.mark.asyncio
async def test_nothing_is_asked_without_an_isis_adjacency_to_name_a_router():
    case, asked = CaseFile(budget=Budget(0, 0)), []

    await record_isis_overload(None, case, call=_call({}, asked))

    assert asked == [] and not [ev for ev in case.evidence if ev["kind"] == KIND]
    assert overload_reads_line(case) is None


@pytest.mark.asyncio
async def test_the_run_details_say_where_it_was_read_how_many_routers_and_how_many_are_set():
    none_set, asked = _case((A, B)), []
    await record_isis_overload(None, none_set, call=_call({(A, COMMAND): _database()}, asked))
    assert "**IS-IS overload bit:** read on `a-data-sw`: 3 routers, none set" in "\n".join(format_run_details(none_set))

    two, asked = _case((A, B)), []
    await record_isis_overload(None, two, call=_call({(A, COMMAND): _database(overloaded=(B, C))}, asked))
    assert "read on `a-data-sw`: 3 routers, 2 set" in "\n".join(format_run_details(two))

    unread, asked = _case((A, B)), []
    await record_isis_overload(None, unread, call=_call({}, asked))
    assert "**IS-IS overload bit:** not read (asked `a-data-sw`, `b-data-sw`)" in "\n".join(format_run_details(unread))


@pytest.mark.asyncio
async def test_a_set_bit_is_one_follow_up_item_and_a_result_line_and_never_a_status():
    case, asked = _case((A, B)), []
    await record_isis_overload(None, case, call=_call({(A, COMMAND): _database(overloaded=(B, C))}, asked))

    assert format_followup_operator(case) == [
        "1. Check whether the IS-IS overload bit is intended on `b-data-sw`, `c-data-sw`: each advertises it in the "
        "database read from `a-data-sw`, and IS-IS routers avoid sending transit through a router that does. "
        "No service status rests on it."]
    result = "\n".join(format_result_section(case, None))
    assert "- **IS-IS overload:** 2 routers advertise the overload bit (a lead, not a status)." in result
    assert not case.issues                                                    # no issue, so no drill and no count

    quiet, asked = _case((A, B)), []
    await record_isis_overload(None, quiet, call=_call({(A, COMMAND): _database()}, asked))
    assert format_followup_operator(quiet) == ["(none)"]
    assert "IS-IS overload" not in "\n".join(format_result_section(quiet, None))
