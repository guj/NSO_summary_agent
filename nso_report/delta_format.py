"""Delta text helpers for executive / report sections."""

from __future__ import annotations

from typing import Any


def format_delta_section(delta: dict[str, Any]) -> str:
    """Fixed wording for delta block."""
    if delta.get("first_run"):
        return "First run — no previous snapshot to compare."

    if not delta_has_changes(delta):
        return "No delta to report"

    lines: list[str] = []
    for service_type in sorted(delta.get("counts") or {}):
        diff = delta["counts"][service_type]
        parts = [
            f"{field} {value:+d}"
            for field in ("total", "up", "down", "degraded", "unknown")
            if (value := int(diff.get(field, 0))) != 0
        ]
        if parts:
            lines.append(f"- {service_type}: {', '.join(parts)}")

    for item in delta.get("status_changes") or []:
        lines.append(
            f"- {item['name']}: {item['from']} -> {item['to']}"
        )

    for name in delta.get("new_failures") or []:
        lines.append(f"- New failure: {name}")

    for name in delta.get("recoveries") or []:
        lines.append(f"- Recovered: {name}")

    for name in delta.get("removed") or []:
        lines.append(f"- Removed: {name}")

    return "\n".join(lines) if lines else "No delta to report"


def delta_has_changes(delta: dict[str, Any]) -> bool:
    if delta.get("first_run"):
        return True
    if delta.get("counts"):
        return True
    for key in ("new_failures", "recoveries", "status_changes", "removed"):
        if delta.get(key):
            return True
    return False
