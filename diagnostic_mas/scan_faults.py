"""What a saved scan writes to ``faults.json``: its faults as new, still open, recovered or not rechecked.

For a script such as a ticketing tool: open a ticket for a new fault, keep one for a fault still
open, close one only for a recovered fault, and leave one alone when its fault was not rechecked.
Built from what the report already uses (the final service status, the routing faults and their
history, the comparison with the previous scan); nothing here changes a status or a check.
Bump ``FORMAT_VERSION`` when a field changes meaning or goes away; adding a field does not need it.
"""
from __future__ import annotations

from typing import Any

from diagnostic_mas.case import CaseFile
from diagnostic_mas.case_delta import is_problem
from diagnostic_mas import newness as scan_newness
from diagnostic_mas.item_history import CLEAR, ROUTING_FAULT_CODES, SEEN, UNREAD, routing_lead
from diagnostic_mas.newness import Newness, Seen, newness_for
from diagnostic_mas.service_final_status import STATUSES, final_service_assessments, final_service_counts
from nso_facts.topology.graph import ends_from_edge_id
from nso_facts.topology.underlay import UNREAD_INTERFACE

FORMAT_VERSION = 1
# Final service statuses that are faults; Unknown is too little evidence to be one, so is only counted.
_OPEN_SERVICE = ("down", "degraded")
_PREVIOUS = {scan_newness.NEW: "not open", scan_newness.CARRIED: "open", scan_newness.UNREAD: "not read"}
_NEW = {"not open", "none"}
_ROUTING_KINDS = {"isis": "isis", "bgp": "bgp", "bgp-outside": "outside_bgp"}


def scan_faults(
    case: CaseFile, delta: dict[str, Any] | None, *, run_id: str, previous_run_id: str | None,
    service_type: str | None = None, service_id: str | None = None, report_url: str | None = None,
) -> dict[str, Any]:
    """The scan's faults against the previous saved scan; ``delta`` is None when there was none."""
    newness = newness_for(case, delta)
    open_rows = _open_services(case, newness, run_id) + _open_routing(case, newness, run_id)
    open_keys = {row["key"] for row in open_rows}
    recovered, not_rechecked = _routing_outcomes(case)
    recovered += _cleared_services(delta)
    not_rechecked += _stale_services(case)
    return {
        "format_version": FORMAT_VERSION,
        "run_id": run_id,
        "previous_run_id": previous_run_id,
        "scope": {"kind": "services" if service_type or service_id else "full",
                  "service_type": service_type or None, "service_id": service_id or None},
        "report_url": report_url or None,
        "counts": {"services": _service_counts(case)},
        "new": [row for row in open_rows if row["previous_scan"] in _NEW],
        "still_open": [row for row in open_rows if row["previous_scan"] not in _NEW],
        "recovered": [row for row in recovered if row["key"] not in open_keys],
        "not_rechecked": [row for row in not_rechecked if row["key"] not in open_keys],
    }


def _since(seen: Seen | None, newness: Newness | None, run_id: str) -> dict[str, Any]:
    """What the previous scan saw of an open fault, and the first scan of this stretch when it is known.

    A first sighting, not an onset: a fault "not open" last scan may have been up, Unknown or not there yet.
    """
    if newness is None:
        return {"previous_scan": "none", "first_seen_run_id": run_id, "first_seen_exact": False}
    if seen is None:
        return {"previous_scan": "unknown", "first_seen_run_id": None, "first_seen_exact": False}
    if seen.state == scan_newness.NEW:
        return {"previous_scan": "not open", "first_seen_run_id": run_id, "first_seen_exact": True}
    return {"previous_scan": _PREVIOUS[seen.state], "first_seen_run_id": seen.first_run_id,
            "first_seen_exact": bool(seen.first_run_id) and seen.exact}


def _open_services(case: CaseFile, newness: Newness | None, run_id: str) -> list[dict[str, Any]]:
    rows = []
    for service_type, name, _service, status, _reason in final_service_assessments(case):
        if status in _OPEN_SERVICE:
            seen = newness.of_service(name) if newness is not None else None
            rows.append({"key": f"service:{name}", "kind": "service", "service": name,
                         "service_type": service_type, "status": status,
                         **_since(seen, newness, run_id)})
    return rows


def _routing_row(edge_id: str) -> dict[str, Any]:
    """The kind and ends of a routing session or adjacency, read from its id."""
    prefix = edge_id.partition(":")[0]
    if prefix == "bgp-outside":
        _, device, address = edge_id.split(":", 2)
        ends = [{"device": device, "address": address}]
    else:
        pair = ends_from_edge_id(edge_id)
        # An interface the scan could not name (its device was not read) is null, not the id's placeholder.
        ends = [{key: (None if key == "interface" and value == UNREAD_INTERFACE else value)
                 for key, value in end.items()} for end in pair if end] if pair else []
    return {"key": edge_id, "kind": _ROUTING_KINDS.get(prefix, prefix), "ends": ends}


def _open_routing(case: CaseFile, newness: Newness | None, run_id: str) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for issue in case.issues:
        code, edge_id = issue.get("code"), str(issue.get("edge_id") or "")
        if code in ROUTING_FAULT_CODES and edge_id and edge_id not in rows and is_problem(issue):
            seen = newness.routing.get(edge_id) if newness is not None else None
            row = _routing_row(edge_id)
            rows[edge_id] = {"key": row["key"], "kind": row["kind"], "status": routing_lead(code), "code": code,
                             "ends": row["ends"], **_since(seen, newness, run_id)}
    return list(rows.values())


def _routing_outcomes(case: CaseFile) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Routing faults read clear this scan after last being seen open, and those this scan did not read."""
    recovered, not_rechecked = [], []
    for key, entry in (case.item_history or {}).items():
        kind, _, edge_id = key.partition("|")
        recent = str((entry or {}).get("recent") or "")
        if kind != "routing" or not recent:
            continue
        row = {**_routing_row(edge_id), "last_seen_run_id": entry.get("last_seen_run_id")}
        if recent[-1] == CLEAR and recent[:-1].rstrip(UNREAD).endswith(SEEN):
            recovered.append(row)
        elif recent[-1] == UNREAD and recent.rstrip(UNREAD).endswith(SEEN):
            not_rechecked.append(row)
    return recovered, not_rechecked


def _service_row(row: dict[str, Any]) -> dict[str, Any]:
    """A service fault that is no longer open this scan, from a fault-history or cleared-fault row."""
    name, last_seen = str(row.get("service")), row.get("last_observed_run_id")
    return {"key": f"service:{name}", "kind": "service", "service": name,
            "service_type": str(row.get("service_type") or ""),
            "was": str(row.get("was") or row.get("status") or ""),
            "last_seen_run_id": None if last_seen in (None, "", "unknown") else last_seen}


def _cleared_services(delta: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [_service_row(row) for row in (delta or {}).get("service_faults_cleared") or [] if row.get("service")]


def _stale_services(case: CaseFile) -> list[dict[str, Any]]:
    """Service faults carried from an earlier scan that this scan did not recheck."""
    return [_service_row(row) for row in case.last_known_service_faults or []
            if row.get("service") and row.get("rechecked") is False]


def _service_counts(case: CaseFile) -> dict[str, int]:
    rows = final_service_counts(case)
    return {status: sum(int(row[status]) for row in rows.values()) for status in STATUSES}
