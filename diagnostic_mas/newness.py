"""Whether each reported problem is new this scan or carried over from the previous one.

Read from the comparison with the previous saved scan, for the Result list's tags and the
follow-up items' labels. A fact about the saved scans: nothing here changes a status or a check.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

NEW, CARRIED, UNREAD = "new", "carried", "unread"


class Seen(NamedTuple):
    """One problem against the previous scan; a carried problem seen in every scan that read it has a first day."""
    state: str
    first_run_id: str | None = None
    exact: bool = False


@dataclass(frozen=True)
class Newness:
    """The previous scan's view of this scan's problems: services by name, routing faults by edge."""
    services: Mapping[str, Seen] = field(default_factory=dict)
    routing: Mapping[str, Seen] = field(default_factory=dict)
    new_issue_keys: frozenset[str] = frozenset()

    def of_service(self, name: str) -> Seen | None:
        return self.services.get(str(name))

    def of_issue(self, issue: dict[str, Any]) -> Seen:
        from diagnostic_mas.case_delta import issue_key

        edge = str(issue.get("edge_id") or "")
        if edge in self.routing:
            return self.routing[edge]
        return Seen(NEW) if issue_key(issue) in self.new_issue_keys else Seen(CARRIED)


def _routing_seen(entry: dict[str, Any]) -> Seen:
    """From an item history entry whose last outcome is this scan's."""
    from diagnostic_mas.item_history import CLEAR, SEEN

    recent = str(entry.get("recent") or "")
    if len(recent) < 2:
        # First seen this scan: new when the previous scan read it in good order.
        return Seen(NEW) if entry.get("first_seen_exact") else Seen(UNREAD)
    if recent[-2] == CLEAR:
        return Seen(NEW)
    if recent[-2] != SEEN:
        return Seen(UNREAD)
    if int(entry.get("seen") or 0) != int(entry.get("read") or 0):
        return Seen(CARRIED)
    return Seen(CARRIED, entry.get("first_seen_run_id"), bool(entry.get("first_seen_exact")))


def newness_for(case: Any, delta: dict[str, Any] | None) -> Newness | None:
    """This scan's problems against the previous scan; None when there was none to compare with."""
    if delta is None:
        return None
    from diagnostic_mas.case_delta import issue_key
    from diagnostic_mas.item_history import ROUTING_FAULT_CODES, routing_key

    services = {str(row["service"]): Seen(CARRIED, row.get("first_observed_run_id"),
                                          bool(row.get("first_observed_exact")))
                for row in getattr(case, "last_known_service_faults", None) or []
                if isinstance(row, dict) and row.get("service")}
    services.update({str(row["service"]): Seen(NEW) for row in delta.get("newly_failed_services") or []
                     if isinstance(row, dict) and row.get("service")})
    history = getattr(case, "item_history", None) or {}
    routing = {}
    for issue in getattr(case, "issues", None) or []:
        edge = str(issue.get("edge_id") or "") if isinstance(issue, dict) else ""
        entry = history.get(routing_key(edge)) if edge else None
        if issue.get("code") in ROUTING_FAULT_CODES and isinstance(entry, dict):
            routing[edge] = _routing_seen(entry)
    return Newness(services=services, routing=routing,
                   new_issue_keys=frozenset(issue_key(issue) for issue in delta.get("new_problems") or []
                                            if isinstance(issue, dict)))


def _day(seen: Seen) -> str:
    from diagnostic_mas.open_faults import run_day

    return run_day(seen.first_run_id) + ("" if seen.exact else " or earlier")


def label(seen: Iterable[Seen | None]) -> str | None:
    """``New this scan``, ``Open since 27 Oct or earlier``, ``Carried over`` or ``Not read last scan``.

    None when any member's state is not known or the members differ.
    """
    seen = list(seen)
    if not seen or any(s is None for s in seen):
        return None
    states = {s.state for s in seen}
    if states == {NEW}:
        return "New this scan"
    if states == {UNREAD}:
        return "Not read last scan"
    if states != {CARRIED}:
        return None
    if not all(s.first_run_id for s in seen):
        return "Carried over"
    ordered = sorted(seen, key=lambda s: (str(s.first_run_id), s.exact))
    first, last = _day(ordered[0]), _day(ordered[-1])
    return f"Open since {first}" + (f"; some since {last}" if last != first else "")


def tag(seen: list[Seen], names: list[str], *, word: str = "new") -> str:
    """``(none new)``, ``(not new)``, ``(1 new: `a` ↔ `b`)`` or ``(not read last scan)`` for one Result phrase."""
    new = [name for s, name in zip(seen, names) if s.state == NEW]
    unread = sum(1 for s in seen if s.state == UNREAD)
    if unread and unread == len(seen):
        return "(not read last scan)"
    if new and all(new):
        shown = ", ".join(new[:3]) + (f", and {len(new) - 3} more" if len(new) > 3 else "")
        parts = [f"{len(new)} {word}: {shown}"]
    elif new:
        parts = [f"{len(new)} {word}"]
    else:
        parts = [f"not {word}" if len(seen) == 1 else f"none {word}"]
    if unread:
        parts.append(f"{unread} not read last scan")
    return "(" + "; ".join(parts) + ")"


def order_new_first(items: list[tuple[str | None, str]]) -> list[str]:
    """Labeled items, new ones first and the rest in their order: ``**New this scan:** …``."""
    ranked = sorted(items, key=lambda item: item[0] != "New this scan")
    return [f"**{name}:** {text}" if name else text for name, text in ranked]
