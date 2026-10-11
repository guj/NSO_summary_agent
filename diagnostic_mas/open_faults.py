"""Open service faults for the top of a report: what changed, and how old the rest is.

Faults that one port or cut-off device explains are shown as one row. Age comes
from two clocks: the scan that first listed the fault, and the time the device
itself gives for a down link in this scan's own readings.
"""
from __future__ import annotations

import re
from typing import Any

from diagnostic_mas.case import CaseFile

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def run_day(run_id: Any) -> str:
    """``5 Oct`` for a scan id that carries its start time; otherwise the id itself."""
    stamp = re.fullmatch(r"\d{4}(\d{2})(\d{2})T\d{6}Z", str(run_id or ""))
    if not stamp or not 1 <= int(stamp[1]) <= 12:
        return f"`{run_id}`"
    return f"{int(stamp[2])} {_MONTHS[int(stamp[1]) - 1]}"


def short_id(name: Any) -> str:
    """A service id cut to its first block (``cs-f8e455d7…``); other names unchanged."""
    cut = re.fullmatch(r"(.+?-[0-9a-f]{8})(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", str(name))
    return cut[1] + "…" if cut else str(name)


def age_days(text: Any) -> int | None:
    """Whole days in a device uptime such as ``8w0d``, ``1y12w`` or ``04:12:33``; None if it is not one."""
    text = str(text or "")
    if re.fullmatch(r"\d+:\d{2}:\d{2}", text):
        return 0
    parts = re.fullmatch(r"(?:(\d+)y)?(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?", text)
    if not text or not parts:
        return None
    years, weeks, days, _hours = (int(part or 0) for part in parts.groups())
    return years * 365 + weeks * 7 + days


def _period(days: int) -> tuple[int, str]:
    return (days // 7, "week") if days >= 14 else (days, "day")


def _span(days: list[int]) -> str:
    def words(count: int, unit: str) -> str:
        return "under a day" if (count, unit) == (0, "day") else f"{count} {unit}{'' if count == 1 else 's'}"

    low, high = _period(min(days)), _period(max(days))
    if low == high:
        return words(*low)
    return f"{low[0] if low[1] == high[1] else words(*low)} to {words(*high)}"


def _age_clause(ages: list[str]) -> str:
    days = [d for d in map(age_days, ages) if d is not None]
    never = sum(1 for age in ages if age == "never")
    parts = []
    if days:
        parts.append(("Links" if len(days) > 1 else "Link") + " down " + _span(days))
    if never:
        parts.append(f"{never} never came up" if days else f"{never} link{'' if never == 1 else 's'} never came up")
    return "; ".join(parts) + "." if parts else ""


def _group(where: str, members: list[dict[str, Any]], ages: list[str], *, single: bool = False) -> dict[str, Any]:
    kinds = {str(m.get("service_type") or "") for m in members}
    # The group is as old as its oldest fault; an inexact date wins a tie.
    dated = sorted((str(m["first_observed_run_id"]), bool(m.get("first_observed_exact")))
                   for m in members if m.get("first_observed_run_id"))
    first, exact = dated[0] if dated else (None, False)
    return {
        "where": where, "single": single, "services": [str(m["service"]) for m in members],
        "service_type": str(members[0].get("service_type") or "") if single else "",
        "kind": kinds.pop() if len(kinds) == 1 and "" not in kinds else "services",
        "status": "down" if any(m.get("status") == "down" for m in members) else str(members[0].get("status") or ""),
        "first_observed_run_id": first, "first_observed_exact": exact and len(dated) == len(members),
        "rechecked": all(m.get("rechecked", True) for m in members), "link_ages": list(ages),
    }


def open_fault_groups(case: CaseFile, rows: list[dict[str, Any]], *, new: Any = ()) -> list[dict[str, Any]]:
    """The open faults as report rows: one per port or cut-off device that explains them, else one per service.

    ``new`` names the faults found this scan: they are reported as new, not also as still open.
    """
    from diagnostic_mas.port_investigation import DEVICE_SOURCE, SOURCE, device_findings, port_findings

    found_now = {str(name) for name in new}
    left = {str(r["service"]): r for r in rows
            if isinstance(r, dict) and r.get("service") and str(r["service"]) not in found_now}
    groups: list[dict[str, Any]] = []
    for finding in port_findings(case):
        links = finding.get("links") or {}
        on_link = {link: [n for n in names if (left.get(n) or {}).get("source") == SOURCE]
                   for link, names in links.items()}
        members = [left.pop(name) for name in dict.fromkeys(n for names in on_link.values() for n in names)]
        if members:
            ages = [age for link, age in (finding.get("link_ages") or {}).items() if on_link.get(link)]
            groups.append(_group(f"`{finding.get('device')}` port {finding.get('port')}", members, ages))
    for finding in device_findings(case):
        members = [left.pop(name) for name in dict.fromkeys(finding.get("services") or [])
                   if (left.get(name) or {}).get("source") == DEVICE_SOURCE]
        if members:
            groups.append(_group(f"`{finding.get('device')}` cut off", members, []))
    groups.extend(_group("", [row], [], single=True) for row in left.values())
    return groups


def _label(row: dict[str, Any], *, short: bool = False) -> str:
    name = short_id(row.get("service")) if short else row.get("service")
    return f"`{row['service_type']}/{name}`" if row.get("service_type") else f"`{name}`"


def format_open_faults(groups: list[dict[str, Any]], *, limit: int = 8, short: bool = False) -> list[str]:
    """One line per open fault or group; ``short`` cuts long service ids for a notification."""
    lines = []
    for group in groups[:limit]:
        status = str(group["status"]).capitalize()
        if group["single"]:
            label = _label({"service": group["services"][0], "service_type": group["service_type"]}, short=short)
            line = f"- {label} {status}" + ("" if group["rechecked"] else ", not rechecked this scan") + "."
        else:
            line = f"- {group['where']}: {len(group['services'])} {group['kind']} {status}."
        if group["first_observed_run_id"]:
            line += (f" First seen {run_day(group['first_observed_run_id'])}"
                     + ("" if group["first_observed_exact"] else " or earlier") + ".")
        ages = _age_clause(group["link_ages"])
        lines.append(line + (" " + ages if ages else ""))
    if len(groups) > limit:
        lines.append(f"- …and {len(groups) - limit} more")
    return lines


def _listed(items: list[str], limit: int) -> str:
    if not items:
        return "none"
    more = f", +{len(items) - limit} more" if len(items) > limit else ""
    return f"{len(items)} — " + ", ".join(items[:limit]) + more


def digest_changes(delta: dict[str, Any] | None, previous_run_id: str | None,
                   groups: list[dict[str, Any]], *, limit: int = 5) -> list[str]:
    """The lines that lead the notification: changes since the previous scan, then what is still open."""
    from diagnostic_mas.case_delta import _interface_lines, _routing_session_line, _status_change_line

    if delta is None:
        lines = ["No previous scan to compare with."]
    else:
        lines = ["**Since the previous scan**" + (f" (`{previous_run_id}`):" if previous_run_id else ":")]
        lines.append("- New service faults: " + _listed(
            [f"{_label(r, short=True)} {r.get('status')}" for r in delta.get("newly_failed_services") or []], limit))
        lines.append("- Cleared: " + _listed(
            [_label(r, short=True) for r in delta.get("service_faults_cleared") or []], limit))
        moved = list(delta.get("status_changed_on_unchanged_checks") or [])
        if moved:
            lines.append(f"- Status changed on unchanged checks: {len(moved)} — "
                         + "; ".join(_status_change_line(row).replace(" — ", " ") for row in moved[:3])
                         + (f"; +{len(moved) - 3} more" if len(moved) > 3 else ""))
        back = [f"`{device}`" for device in delta.get("recovered_devices") or []]
        if back:
            lines.append("- Devices answering again: " + ", ".join(back))
        missing = len(delta.get("newly_missing_evidence") or [])
        if missing:
            lines.append(f"- Newly missing evidence: {missing} (listed in the full report)")
        links = list(delta.get("interfaces_lost") or [])
        lines.append("- Links up before, down now: " + (
            f"{len(links)} — " + "; ".join(_interface_lines(links)[:3]) if links else "none"))
        gaining = list(delta.get("links_gaining_errors") or [])
        if gaining:
            from diagnostic_mas.port_counters import gain_line

            lines.append(f"- Links gaining errors while up: {len(gaining)} — "
                         + "; ".join(gain_line(row) for row in gaining[:2])
                         + (f"; +{len(gaining) - 2} more" if len(gaining) > 2 else ""))
        sessions = list(delta.get("routing_sessions_lost") or [])
        if sessions:
            lines.append(f"- Routing sessions lost: {len(sessions)} — "
                         + "; ".join(_routing_session_line(row) for row in sessions[:2])
                         + (f"; +{len(sessions) - 2} more" if len(sessions) > 2 else ""))
    lines.append("")
    services = sum(len(group["services"]) for group in groups)
    heading = "**Open service faults:**" if delta is None else "**Still open:**"
    if not groups:
        lines.append(f"{heading} none")
    else:
        causes = f", {len(groups)} cause{'' if len(groups) == 1 else 's'}" if len(groups) < services else ""
        lines.append(f"{heading} {services} service{'' if services == 1 else 's'}{causes}")
        lines.extend(format_open_faults(groups, limit=limit, short=True))
    return lines
