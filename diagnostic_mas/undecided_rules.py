"""Counts for rules the operators have not decided yet.

Reporting only: no status depends on these counts. They show how many services
each open rule would change, from this scan's own results. Remove a rule's
count here once it is decided and built into the fixed checks.
"""
from __future__ import annotations

import re
from typing import Any

from diagnostic_mas.case import CaseFile

_DEFAULT_ROUTES = {"0.0.0.0/0", "::/0"}


def _only_exact_border_route_missing(service: dict[str, Any]) -> bool:
    """True when the fixed check's only gaps are the service's own prefix in a border router's route table."""
    basic = service.get("basic_checks") or {}
    local = (basic.get("intent") or {}).get("device")
    gaps = [c for c in basic.get("checks") or [] if c.get("status") != "pass"]
    if basic.get("status") != "unknown" or not gaps or not local:
        return False
    for gap in gaps:
        target = re.match(r"IPv[46] (\S+) in ", str(gap.get("observation") or ""))
        if gap.get("check") != "rib" or gap.get("device") == local or not target or target[1] in _DEFAULT_ROUTES:
            return False
    return True


def undecided_rule_counts(case: CaseFile, *, answered: set[str]) -> dict[str, Any]:
    """Services each open rule would change.

    ``covering_route``: l3rt that are Up on the LLM's conclusion where the fixed
    check only lacks the exact prefix on a border router.
    ``cut_off``: per device with no adjacency and no session up, the services
    its cut-off investigation marked Down, and the l3rt naming only that device
    that are still Up.
    """
    from diagnostic_mas.port_investigation import DEVICE_SOURCE, cut_off_devices
    from diagnostic_mas.service_final_status import final_service_assessments

    rows = list(final_service_assessments(case))
    covering = sorted(
        name for kind, name, service, status, reason in rows
        if kind == "l3rt" and status == "up" and reason.startswith("LLM concluded")
        and _only_exact_border_route_missing(service))
    by_rule = {str((dx.get("subject") or {}).get("name") or "") for dx in case.diagnoses
               if dx.get("kind") == "dataplane" and dx.get("source") == DEVICE_SOURCE}
    cut_off = []
    for target in cut_off_devices(case, answered):
        device = target["device"]
        on_device = [(kind, name, {str(d) for d in service.get("devices") or []}, status)
                     for kind, name, service, status, _reason in rows]
        cut_off.append({
            "device": device,
            "down": sum(1 for _kind, name, devices, status in on_device
                        if device in devices and status == "down" and name in by_rule),
            "internal_up": sorted(name for kind, name, devices, status in on_device
                                  if kind == "l3rt" and devices == {device} and status == "up"),
        })
    return {"covering_route": covering, "cut_off": cut_off}


def _names(names: list[str], limit: int = 5) -> str:
    more = f", +{len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(f"`{name}`" for name in names[:limit]) + more


def format_undecided_rules(counts: dict[str, Any] | None) -> list[str]:
    """The report block; empty when no rule would change anything in this scan."""
    counts = counts or {}
    covering, cut_off = list(counts.get("covering_route") or []), list(counts.get("cut_off") or [])
    if not covering and not cut_off:
        return []
    lines = ["## Undecided rules", "",
             "What each open rule would change. No status in this report depends on these counts.", ""]
    if covering:
        one = len(covering) == 1
        lines.append(
            f"- **Border route:** {len(covering)} l3rt {'is' if one else 'are'} Up because the LLM accepted a "
            "covering route where the fixed check wants the service's exact prefix on each border router: "
            f"{_names(covering)}. If the exact prefix is required {'it becomes' if one else 'they become'} "
            f"Unknown; if a covering route is enough {'it passes' if one else 'they pass'} without the LLM.")
    for row in cut_off:
        down, internal = int(row.get("down") or 0), list(row.get("internal_up") or [])
        line = (f"- **Cut-off device `{row.get('device')}`:** {down} service{'' if down == 1 else 's'} "
                "Down under the current rule.")
        if internal:
            line += (f" {len(internal)} internal l3rt {'is' if len(internal) == 1 else 'are'} Up and would be Down "
                     f"if an l3rt must reach other sites: {_names(internal)}.")
        lines.append(line)
    return lines + [""]


def digest_undecided_rules(counts: dict[str, Any] | None, previous: dict[str, Any] | None) -> list[str]:
    """Notification lines: a cut-off device every time, the border-route count only when it changes."""
    counts = counts or {}
    lines = []
    for row in counts.get("cut_off") or []:
        internal = len(row.get("internal_up") or [])
        line = f"- `{row.get('device')}` cut off: {int(row.get('down') or 0)} Down under the current rule"
        if internal:
            line += (f"; {internal} internal l3rt {'is' if internal == 1 else 'are'} Up and would be Down "
                     "if an l3rt must reach other sites")
        lines.append(line + ".")
    now = len(counts.get("covering_route") or [])
    before = len(previous["covering_route"]) if isinstance((previous or {}).get("covering_route"), list) else None
    if now != before and (now or before):
        lines.append(f"- Border route: {now} l3rt Up on a covering route "
                     + ("(first count)." if before is None else f"(was {before})."))
    return ["**Undecided rules:**", *lines] if lines else []
