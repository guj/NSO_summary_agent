"""Collect and format per-device hardware health from MCP get_hardware_health."""

from __future__ import annotations

from typing import Any

from nso_facts.mcp_client import call_mcp


async def collect_hardware_health(
    client: Any,
    device_names: list[str],
) -> dict[str, dict[str, Any]]:
    """Return ``{device: hardware_data | {error}}`` without ``raw`` blobs."""
    out: dict[str, dict[str, Any]] = {}
    for device in device_names:
        try:
            result = await call_mcp(
                client,
                "get_hardware_health",
                {"device_name": device},
            )
        except Exception as exc:  # noqa: BLE001
            out[device] = {"error": str(exc)}
            continue
        if isinstance(result, dict) and result.get("status") == "error":
            out[device] = {
                "error": str(
                    result.get("error_message") or "get_hardware_health failed"
                )
            }
            continue
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            data = result if isinstance(result, dict) else None
        if not isinstance(data, dict):
            out[device] = {"error": "unexpected get_hardware_health shape"}
            continue
        stored = {
            k: data[k]
            for k in (
                "device",
                "outcome",
                "summary",
                "temperature",
                "fans",
                "power",
                "control_plane",
            )
            if k in data
        }
        out[device] = stored
    return out


def fleet_hardware_alert_counts(hardware_health: Any) -> tuple[int, int, int, int]:
    """Return (temp, fan, power, control_plane_drop) device alert counts."""
    if not isinstance(hardware_health, dict):
        return 0, 0, 0, 0
    temp_a = fan_a = pwr_a = cp_a = 0
    for entry in hardware_health.values():
        if not isinstance(entry, dict) or entry.get("error"):
            continue
        temps = entry.get("temperature")
        if (
            isinstance(temps, list)
            and temps
            and any(isinstance(x, dict) and x.get("ok") is False for x in temps)
        ):
            temp_a += 1
        fans = entry.get("fans")
        if (
            isinstance(fans, list)
            and fans
            and any(isinstance(x, dict) and x.get("ok") is False for x in fans)
        ):
            fan_a += 1
        power = entry.get("power")
        if (
            isinstance(power, list)
            and power
            and any(isinstance(x, dict) and x.get("ok") is False for x in power)
        ):
            pwr_a += 1
        cps = entry.get("control_plane")
        if isinstance(cps, list) and cps:
            drops = 0
            for row in cps:
                if isinstance(row, dict):
                    try:
                        drops += int(row.get("dropped") or 0)
                    except (TypeError, ValueError):
                        pass
            if drops > 0:
                cp_a += 1
    return temp_a, fan_a, pwr_a, cp_a


def device_hardware_label(entry: dict[str, Any] | None) -> str:
    """Device Health table label: Healthy / Review / Unavailable."""
    if not isinstance(entry, dict) or entry.get("error"):
        return "Unavailable"

    temps = entry.get("temperature") if isinstance(entry.get("temperature"), list) else []
    fans = entry.get("fans") if isinstance(entry.get("fans"), list) else []
    power = entry.get("power") if isinstance(entry.get("power"), list) else []
    cps = (
        entry.get("control_plane")
        if isinstance(entry.get("control_plane"), list)
        else []
    )
    if not temps and not fans and not power and not cps:
        return "Unavailable"

    if any(isinstance(x, dict) and x.get("ok") is False for x in temps):
        return "Review"
    if any(isinstance(x, dict) and x.get("ok") is False for x in fans):
        return "Review"
    if any(isinstance(x, dict) and x.get("ok") is False for x in power):
        return "Review"
    drops = 0
    for row in cps:
        if isinstance(row, dict):
            try:
                drops += int(row.get("dropped") or 0)
            except (TypeError, ValueError):
                pass
    if drops > 0:
        return "Review"
    return "Healthy"


def format_device_hardware_section(entry: dict[str, Any] | None) -> list[str]:
    i1, i2, i3 = "  ", "    ", "      "
    lines = ["Hardware", "--------"]
    if not isinstance(entry, dict) or entry.get("error"):
        for title in ("Temperature", "Fans", "Power Supplies", "Control Plane"):
            lines.append(f"{i1}{title}")
            lines.append(f"{i2}Unavailable")
            lines.append("")
        if lines[-1] == "":
            lines.pop()
        return lines

    lines.extend(_format_temperature(entry.get("temperature"), i1, i2, i3))
    lines.append("")
    lines.extend(_format_fans(entry.get("fans"), i1, i2, i3))
    lines.append("")
    lines.extend(_format_power(entry.get("power"), i1, i2, i3))
    lines.append("")
    lines.extend(_format_control_plane(entry.get("control_plane"), i1, i2, i3))
    return lines


def _category_unavailable(title: str, i1: str, i2: str) -> list[str]:
    return [f"{i1}{title}", f"{i2}Unavailable"]


def _temp_bucket(status: str) -> str:
    s = (status or "").lower()
    if s == "normal":
        return "normal"
    if s == "minor":
        return "warning"
    if s in {"major", "critical"}:
        return "critical"
    if s:
        return "warning"
    return "normal"


def _format_temperature(
    rows: Any, i1: str = "  ", i2: str = "    ", i3: str = "      "
) -> list[str]:
    if not isinstance(rows, list) or not rows:
        return _category_unavailable("Temperature", i1, i2)
    normal = warning = critical = 0
    warn_items: list[str] = []
    crit_items: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        bucket = _temp_bucket(str(row.get("status") or ""))
        if bucket == "normal":
            normal += 1
        elif bucket == "warning":
            warning += 1
            warn_items.append(_temp_label(row))
        else:
            critical += 1
            crit_items.append(_temp_label(row))
    if warning == 0 and critical == 0:
        return [
            f"{i1}Temperature",
            f"{i2}Normal Sensors: {normal}, Warning: {warning}, Critical: {critical}",
        ]
    out = [f"{i1}Temperature"]
    if crit_items:
        out.append(f"{i2}Critical:")
        out.extend(f"{i3}{x}" for x in crit_items)
    if warn_items:
        out.append(f"{i2}Warning:")
        out.extend(f"{i3}{x}" for x in warn_items)
    return out


def _temp_label(row: dict[str, Any]) -> str:
    name = str(row.get("sensor") or row.get("location") or "?")
    if row.get("value_celsius") is not None:
        return f"{name}: {row['value_celsius']}°C"
    return name


def _format_fans(
    rows: Any, i1: str = "  ", i2: str = "    ", i3: str = "      "
) -> list[str]:
    if not isinstance(rows, list) or not rows:
        return _category_unavailable("Fans", i1, i2)
    total = len(rows)
    healthy = sum(1 for r in rows if isinstance(r, dict) and r.get("ok") is True)
    failed_rows = [r for r in rows if isinstance(r, dict) and r.get("ok") is False]
    if not failed_rows:
        return [
            f"{i1}Fans",
            f"{i2}Total: {total}, Healthy: {healthy}, Failed: 0",
        ]
    out = [f"{i1}Fans", f"{i2}Failed:"]
    for r in failed_rows:
        out.append(f"{i3}{r.get('name') or r.get('location') or '?'}")
    return out


def _format_power(
    rows: Any, i1: str = "  ", i2: str = "    ", i3: str = "      "
) -> list[str]:
    if not isinstance(rows, list) or not rows:
        return _category_unavailable("Power Supplies", i1, i2)
    total = len(rows)
    healthy = sum(1 for r in rows if isinstance(r, dict) and r.get("ok") is True)
    failed_rows = [r for r in rows if isinstance(r, dict) and r.get("ok") is False]
    if not failed_rows:
        return [
            f"{i1}Power Supplies",
            f"{i2}Installed: {total}, Healthy: {healthy}, Failed: 0",
        ]
    out = [f"{i1}Power Supplies", f"{i2}Failed:"]
    for r in failed_rows:
        out.append(f"{i3}{r.get('name') or r.get('location') or '?'}")
    return out


def _format_control_plane(
    rows: Any, i1: str = "  ", i2: str = "    ", i3: str = "      "
) -> list[str]:
    if not isinstance(rows, list) or not rows:
        return _category_unavailable("Control Plane", i1, i2)
    drops = 0
    for row in rows:
        if isinstance(row, dict):
            try:
                drops += int(row.get("dropped") or 0)
            except (TypeError, ValueError):
                pass
    if drops == 0:
        return [f"{i1}Control Plane", f"{i2}Drops: 0"]
    return [
        f"{i1}Control Plane",
        f"{i2}Drops:",
        f"{i3}{drops:,} packets",
    ]
