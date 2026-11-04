"""How long and how often routing faults, links gaining errors and IS-IS changes have been seen.

Each scan gives every tracked item one outcome against the scan before it: seen, read clear, or
not read. A scan that did not read an item counts neither way. The record is copied forward
from case to case and built once from the saved scans when the previous case has none. It is
a fact about the saved scans, never a diagnosis: nothing here changes a status or a check.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from diagnostic_mas.case_delta import (
    _adjacency_change_times, _routing_edges, _seen_up_at, _unmatched_rows, adjacency_change_outcomes, is_problem,
    load_case_dict)
from diagnostic_mas.open_faults import run_day
from diagnostic_mas.port_counters import _saved, port_comparisons

# Scans an item's recent outcomes cover; one with no "seen" among them is dropped.
WINDOW = 20
# Saved scans read at most to build a missing record.
BACKFILL_LIMIT = 30
# The routing faults the report counts: BGP down or not established, IS-IS adjacency down.
ROUTING_FAULT_CODES = frozenset({
    "session_down", "configured_no_session", "outside_bgp_session_not_established",
    "configured_no_adjacency", "unidirectional_adjacency"})
SEEN, CLEAR, UNREAD = "S", "C", "-"


def routing_key(edge_id: str) -> str:
    return f"routing|{edge_id}"


def link_key(device: str, port: str) -> str:
    return f"link|{device}:{port}"


def change_key(edge_id: str) -> str:
    return f"isis_change|{edge_id}"


def routing_lead(code: str) -> str:
    """What a routing fault is called in its clause."""
    return "not established" if code in {"configured_no_session", "outside_bgp_session_not_established"} else "down"


def _outside_row(case: dict[str, Any], edge_id: str) -> dict[str, Any] | None:
    _, device, address = edge_id.split(":", 2)
    return (_unmatched_rows(case) or {}).get((device, address))


def _routing_state(case: dict[str, Any], edge_id: str) -> dict[str, Any] | None:
    return (_routing_edges(case, edge_id.split(":", 1)[0]) or {}).get(edge_id)


def _read_up(case: dict[str, Any], edge_id: str) -> bool:
    if edge_id.startswith("bgp-outside:"):
        return (_outside_row(case, edge_id) or {}).get("state") == "established"
    state = _routing_state(case, edge_id) or {}
    return state.get("status") == "up" or _seen_up_at(state) is not None


def read_in(case: dict[str, Any], key: str) -> bool:
    """Whether one saved scan read the item's subject in good order: a routing session or adjacency
    up, a port up, an IS-IS adjacency up. Used to tell whether a first sighting can be dated exactly."""
    kind, _, subject = key.partition("|")
    if kind == "routing":
        return _read_up(case, subject)
    if kind == "link":
        return ((_saved(case).get("ports") or {}).get(subject) or {}).get("state") == "up"
    if kind == "isis_change":
        # Only a scan that read a change time could have seen a change.
        state = (_routing_edges(case, "isis") or {}).get(subject) or {}
        return (subject in _adjacency_change_times(case)
                and (state.get("status") == "up" or _seen_up_at(state) is not None))
    return False


def scan_outcomes(older: dict[str, Any] | None, newer: dict[str, Any],
                  tracked: Iterable[str] = ()) -> dict[str, str]:
    """An outcome for every item ``newer`` saw and every tracked item; links and IS-IS changes need ``older``."""
    tracked = set(tracked)
    out: dict[str, str] = {}
    for issue in newer.get("issues") or []:
        if (isinstance(issue, dict) and is_problem(issue) and issue.get("code") in ROUTING_FAULT_CODES
                and issue.get("edge_id")):
            out[routing_key(str(issue["edge_id"]))] = SEEN
    for key in tracked - out.keys():
        kind, _, subject = key.partition("|")
        if kind == "routing":
            out[key] = CLEAR if _read_up(newer, subject) else UNREAD
    if older is not None:
        pairs = [("link|" + port, gained) for port, gained in port_comparisons(older, newer).items()]
        pairs += [(change_key(edge), moved) for edge, moved in adjacency_change_outcomes(older, newer).items()]
        for key, happened in pairs:
            if happened or key in tracked:
                out[key] = SEEN if happened else CLEAR
    for key in tracked:
        out.setdefault(key, UNREAD)
    return out


def update_history(history: dict[str, dict[str, Any]], outcomes: dict[str, str], run_id: str, *,
                   previous: dict[str, Any] | None, compared: bool = True) -> dict[str, dict[str, Any]]:
    """The record after one more scan.

    A new item's first sighting is exact when ``previous`` read it in good order. For links and
    IS-IS changes that also needs ``compared``: the previous scan was itself compared with the
    one before it, so it would have seen the item.
    """
    out: dict[str, dict[str, Any]] = {}
    for key, entry in history.items():
        outcome = outcomes.get(key, UNREAD)
        recent = (str(entry.get("recent") or "") + outcome)[-WINDOW:]
        if SEEN not in recent:
            continue
        row = dict(entry, recent=recent, scans=int(entry.get("scans") or 0) + 1)
        if outcome in (SEEN, CLEAR):
            row["read"] = int(entry.get("read") or 0) + 1
        if outcome == SEEN:
            row.update(seen=int(entry.get("seen") or 0) + 1, last_seen_run_id=run_id)
        out[key] = row
    for key, outcome in outcomes.items():
        if outcome == SEEN and key not in history:
            pairwise = not key.startswith("routing|")
            out[key] = {"first_seen_run_id": run_id,
                        "first_seen_exact": (previous is not None and (compared or not pairwise)
                                             and read_in(previous, key)),
                        "last_seen_run_id": run_id, "seen": 1, "read": 1, "scans": 1, "recent": SEEN}
    return out


def backfill_history(runs_dir: Path | str, *, before: str,
                     limit: int = BACKFILL_LIMIT) -> dict[str, dict[str, Any]]:
    """The record as the last ``limit`` saved scans before ``before`` give it, oldest first.

    A saved scan that cannot be read breaks the chain: what came before it is dropped and the
    walk starts again after it, so no first sighting is dated across the gap.
    """
    return _backfill(runs_dir, before=before, limit=limit)[0]


def _backfill(runs_dir: Path | str, *, before: str, limit: int) -> tuple[dict[str, dict[str, Any]], bool]:
    """The backfilled record, and whether its last scan was compared with the one before it."""
    try:
        runs = sorted(d.name for d in Path(runs_dir).iterdir()
                      if d.is_dir() and d.name < str(before) and (d / "case.json").is_file())[-limit:]
    except OSError:
        return {}, False
    history: dict[str, dict[str, Any]] = {}
    older: dict[str, Any] | None = None
    compared = False
    for run in runs:
        try:
            case = load_case_dict(Path(runs_dir) / run / "case.json")
        except (OSError, ValueError):
            history, older, compared = {}, None, False
            continue
        history = update_history(history, scan_outcomes(older, case, history), run,
                                 previous=older, compared=compared)
        compared, older = older is not None, case
    return history, compared


def history_for_scan(previous_case: dict[str, Any] | None, current: dict[str, Any], *,
                     runs_dir: Path | str, run_id: str) -> dict[str, dict[str, Any]]:
    """This scan's record: the previous case's, or one built from the saved scans, plus this scan."""
    if previous_case is None:
        return update_history({}, scan_outcomes(None, current), run_id, previous=None)
    history = previous_case.get("item_history")
    # A carried record means the previous scan compared with the one before it.
    compared = True
    if not isinstance(history, dict):
        history, compared = _backfill(runs_dir, before=run_id, limit=BACKFILL_LIMIT)
    return update_history(history, scan_outcomes(previous_case, current, history), run_id,
                          previous=previous_case, compared=compared)


def recurrence_clause(entry: dict[str, Any] | None, lead: str, *, dated: bool = True) -> str | None:
    """``down in every scan since 27 Oct or earlier (29)``; None for an item seen once.

    ``dated=False`` leaves the date out, for a line that already gives one.
    """
    if not isinstance(entry, dict) or int(entry.get("seen") or 0) < 2:
        return None
    seen, read, scans = (int(entry.get(name) or 0) for name in ("seen", "read", "scans"))
    day = run_day(entry.get("first_seen_run_id")) + ("" if entry.get("first_seen_exact") else " or earlier")
    if seen == read:
        span = "every scan" if scans <= read else "every scan that read it"
        return f"{lead} in {span} since {day} ({seen})" if dated else f"{lead} in {span} ({seen})"
    recent = str(entry.get("recent") or "")
    count = recent.count(SEEN)
    text = f"{lead} in {count} of the last {count + recent.count(CLEAR)} scans that read it"
    return f"{text}; first seen {day}" if dated else text


def clause_for(history: dict[str, dict[str, Any]] | None, key: str, lead: str, *,
               dated: bool = True) -> str | None:
    return recurrence_clause((history or {}).get(key), lead, dated=dated)


def routing_fault_clause(issue: dict[str, Any], history: dict[str, dict[str, Any]] | None, *,
                         dated: bool = True) -> str | None:
    """How long a routing fault has been seen, from the carried record; None for anything else."""
    code, edge = str(issue.get("code") or ""), issue.get("edge_id")
    if code not in ROUTING_FAULT_CODES or not isinstance(edge, str) or not edge:
        return None
    return clause_for(history, routing_key(edge), routing_lead(code), dated=dated)
