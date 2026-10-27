"""Packets each end of an IS-IS adjacency down sent and received since the previous scan.

The port counters are read once per scan. For each adjacency down, this subtracts the last
published scan's packet counts from this scan's at both ends, so what one end sent can be set
against what the other received over the same time. It is worked out when the scan runs and kept
with it, so the report never needs the earlier scan. Numbers only: the report draws no conclusion.
A port not read in both scans, or whose counters restarted in between, is not compared.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from diagnostic_mas.case import CaseFile, add_evidence
from diagnostic_mas.link_lldp import edge_ports
from diagnostic_mas.link_optics import _ADJACENCY_DOWN

KIND = "link_traffic"


def _counters(case: CaseFile) -> dict[str, Any]:
    found = [ev.get("payload") or {} for ev in case.evidence if ev.get("kind") == "port_counters"]
    return found[-1] if found else {}


def _gained(now: dict[str, Any] | None, before: dict[str, Any] | None, elapsed: float | None) -> dict[str, Any]:
    """Packets in and out since the earlier reading, or why they cannot be subtracted."""
    from diagnostic_mas.port_counters import _age_seconds

    if not now or not all(isinstance(now.get(name), int) for name in ("packets_in", "packets_out")):
        return {"why": "not read"}
    if not before or not all(isinstance(before.get(name), int) for name in ("packets_in", "packets_out")):
        return {"why": "no earlier reading"}
    cleared = now.get("cleared")
    if cleared not in (None, "never"):
        age = _age_seconds(cleared)
        if age is None or elapsed is None or age < elapsed:
            return {"why": "counters restarted"}
    gained = {"in": now["packets_in"] - before["packets_in"], "out": now["packets_out"] - before["packets_out"]}
    return {"why": "counters restarted"} if min(gained.values()) < 0 else gained


def record_link_traffic(case: CaseFile) -> None:
    """Subtract the previous scan's packet counts at both ends of each adjacency down, and keep the result."""
    edges = [str(issue.get("edge_id") or "") for issue in case.issues if issue.get("code") in _ADJACENCY_DOWN]
    now = _counters(case)
    if not edges or not now:
        return
    before = getattr(case, "previous_port_counters", None) or {}
    try:
        elapsed = (datetime.fromisoformat(now["read_at"]) - datetime.fromisoformat(before["read_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        elapsed = None
    links: dict[str, dict[str, Any]] = {}
    for edge in edges:
        links[edge] = {device: _gained((now.get("ports") or {}).get(f"{device}:{port}") if port else None,
                                       (before.get("ports") or {}).get(f"{device}:{port}") if port else None,
                                       elapsed)
                       for device, port in edge_ports(case, edge)}
    add_evidence(case, {"kind": KIND, "role": "physical", "layer": "physical", "payload": {
        "since": before.get("read_at"), "read_at": now.get("read_at"),
        "minutes": round(elapsed / 60) if elapsed is not None else None, "links": links}})


def _saved(case: CaseFile) -> dict[str, Any] | None:
    found = [ev.get("payload") for ev in case.evidence if ev.get("kind") == KIND]
    return found[-1] if found and isinstance(found[-1], dict) else None


def _direction(source: str, target: str, link: dict[str, Any]) -> str:
    sent, got = link.get(source) or {"why": "not read"}, link.get(target) or {"why": "not read"}
    head = f"{source} → {target}"
    if "out" in sent and "in" in got:
        return f"{head}: {sent['out']:,} sent, {got['in']:,} received"
    reasons = [f"{end['why']} on {device}" for device, end in ((source, sent), (target, got)) if "why" in end]
    return f"{head}: not compared ({'; '.join(dict.fromkeys(reasons))})"


def traffic_line(case: CaseFile, edge_id: str) -> str | None:
    """Both directions of an adjacency's link in packets since the previous scan, or None when not worked out."""
    saved = _saved(case)
    link = ((saved or {}).get("links") or {}).get(edge_id)
    ends = edge_ports(case, edge_id)
    if link is None or len(ends) != 2:
        return None
    if not saved.get("since"):
        return "Packets: no earlier scan to compare with."
    minutes = saved.get("minutes")
    took = ("" if minutes is None else f" ({minutes // 60} h {minutes % 60} min)" if minutes >= 60
            else f" ({minutes} min)")
    (a, _), (b, _) = ends
    return f"Packets since the previous scan{took}: {_direction(a, b, link)}; {_direction(b, a, link)}."
