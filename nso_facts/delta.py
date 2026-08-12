"""Delta computation vs last successful snapshot."""

from __future__ import annotations

from typing import Any


def compute_delta(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if previous is None:
        return {
            "first_run": True,
            "counts": {},
            "new_failures": [],
            "recoveries": [],
            "status_changes": [],
            "removed": [],
        }

    delta_counts: dict[str, dict[str, int]] = {}
    cur_counts = current.get("counts") or {}
    prev_counts = previous.get("counts") or {}
    all_types = set(cur_counts) | set(prev_counts)
    for t in all_types:
        cur = cur_counts.get(t) or {}
        prev = prev_counts.get(t) or {}
        diff: dict[str, int] = {}
        for field in ("total", "up", "down", "degraded", "unknown"):
            diff[field] = int(cur.get(field, 0)) - int(prev.get(field, 0))
        if any(v != 0 for v in diff.values()):
            delta_counts[t] = diff

    cur_services = current.get("services") or {}
    prev_services = previous.get("services") or {}

    status_changes: list[dict[str, str]] = []
    new_failures: list[str] = []
    recoveries: list[str] = []
    removed: list[str] = []

    for name, cur in cur_services.items():
        prev = prev_services.get(name)
        if prev is None:
            continue
        cur_status = cur.get("status", "unknown")
        prev_status = prev.get("status", "unknown")
        if cur_status != prev_status:
            status_changes.append(
                {"name": name, "from": prev_status, "to": cur_status}
            )
            if cur_status in ("down", "degraded") and prev_status == "up":
                new_failures.append(name)
            if cur_status == "up" and prev_status in ("down", "degraded"):
                recoveries.append(name)

    for name in prev_services:
        if name not in cur_services:
            removed.append(name)

    return {
        "first_run": False,
        "counts": delta_counts,
        "new_failures": new_failures,
        "recoveries": recoveries,
        "status_changes": status_changes,
        "removed": removed,
    }
