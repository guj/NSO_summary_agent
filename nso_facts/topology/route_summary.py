"""Per-device ``show route summary`` collection and parsing."""

from __future__ import annotations

from typing import Any

from nso_facts.mcp_client import call_mcp, unwrap_mcp_data


async def collect_route_summaries(
    client: Any,
    device_names: list[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Return ``{device: {total, sources}}`` plus issues and coverage."""
    by_device: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []
    queried = 0
    failed = 0

    for device in device_names:
        try:
            result = await call_mcp(
                client,
                "exec_show",
                {"device_name": device, "input_command": "route summary"},
            )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            by_device[device] = {"error": str(exc)}
            issues.append(
                {
                    "severity": "low",
                    "layer": "routing",
                    "code": "route_summary_error",
                    "edge_id": None,
                    "message": f"{device}: route summary failed: {exc}",
                }
            )
            continue

        if isinstance(result, dict) and result.get("status") == "error":
            failed += 1
            message = str(
                result.get("error_message") or "exec_show route summary failed"
            )
            by_device[device] = {"error": message}
            issues.append(
                {
                    "severity": "low",
                    "layer": "routing",
                    "code": "route_summary_error",
                    "edge_id": None,
                    "message": f"{device}: {message}",
                }
            )
            continue

        queried += 1
        text = _exec_show_text(result)
        parsed = parse_route_summary(text)
        by_device[device] = parsed

    coverage = {
        "devices_total": len(device_names),
        "devices_queried": queried,
        "devices_failed": failed,
    }
    return by_device, issues, coverage


def parse_route_summary(text: str) -> dict[str, Any]:
    """Parse XR ``show route summary`` into total + per-source Routes counts."""
    sources: dict[str, int] = {}
    total: int | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("---"):
            continue
        parts = stripped.split()
        if len(parts) < 5:
            continue
        try:
            routes = int(parts[-4])
            int(parts[-3])
            int(parts[-2])
            int(parts[-1])
        except ValueError:
            continue

        source = " ".join(parts[:-4]).strip()
        if not source:
            continue
        lower = source.lower()
        if lower == "route source" or lower.startswith("route source"):
            continue
        if lower == "total":
            total = routes
            continue
        sources[source] = routes

    if total is None:
        total = sum(sources.values())
    return {"total": total, "sources": sources}


def format_route_summary_lines(entry: dict[str, Any] | None) -> list[str]:
    """Devices-section lines: total header + per-source ``name: count`` rows."""
    if not entry:
        return []
    if entry.get("error"):
        return ["  routes: (unavailable)"]
    total = entry.get("total")
    sources = entry.get("sources") or {}
    if not isinstance(sources, dict):
        sources = {}
    lines = [f"  routes: {total}"]
    for name, count in sorted(
        ((str(k), int(v)) for k, v in sources.items() if int(v) > 0),
        key=lambda item: (-item[1], item[0]),
    ):
        lines.append(f"    {name}: {count}")
    return lines


def format_route_summary_line(entry: dict[str, Any] | None) -> str | None:
    """Joined form of ``format_route_summary_lines`` (or None if empty)."""
    lines = format_route_summary_lines(entry)
    return "\n".join(lines) if lines else None


def _exec_show_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    data = unwrap_mcp_data(result)
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        return str(data.get("result") or "")
    return ""
