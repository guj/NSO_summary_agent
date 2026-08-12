"""Collect and parse per-device CPU / memory for Infrastructure Health."""

from __future__ import annotations

import re
from typing import Any

from nso_facts.mcp_client import call_mcp, unwrap_mcp_data

_CPU_UTIL = re.compile(
    r"CPU utilization for one minute:\s*(?P<one>\d+)\s*%\s*;\s*"
    r"five minutes:\s*(?P<five>\d+)\s*%\s*;\s*"
    r"fifteen minutes:\s*(?P<fifteen>\d+)\s*%",
    re.IGNORECASE,
)
_CPU_NODE = re.compile(r"----\s*(?P<node>\S+)\s*----")
_PHYS_MEM = re.compile(
    r"Physical Memory:\s*(?P<total>\d+)\s*M\s+total\s+"
    r"\((?P<available>\d+)\s*M\s+available\)",
    re.IGNORECASE,
)


async def collect_system_health(
    client: Any,
    device_names: list[str],
) -> dict[str, dict[str, Any]]:
    """Return ``{device: {cpu, memory} | {error}}``."""
    out: dict[str, dict[str, Any]] = {}
    for device in device_names:
        entry: dict[str, Any] = {}
        errors: list[str] = []

        try:
            cpu_result = await call_mcp(
                client,
                "exec_show",
                {"device_name": device, "input_command": "processes cpu"},
            )
            cpu_text = _exec_show_text(cpu_result)
            if isinstance(cpu_result, dict) and cpu_result.get("status") == "error":
                errors.append(
                    str(cpu_result.get("error_message") or "processes cpu failed")
                )
            else:
                cpu = parse_processes_cpu(cpu_text)
                if cpu:
                    entry["cpu"] = cpu
                else:
                    errors.append("could not parse processes cpu header")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"processes cpu: {exc}")

        try:
            mem_result = await call_mcp(
                client,
                "exec_show",
                {"device_name": device, "input_command": "memory summary"},
            )
            mem_text = _exec_show_text(mem_result)
            if isinstance(mem_result, dict) and mem_result.get("status") == "error":
                errors.append(
                    str(mem_result.get("error_message") or "memory summary failed")
                )
            else:
                memory = parse_memory_summary(mem_text)
                if memory:
                    entry["memory"] = memory
                else:
                    errors.append("could not parse memory summary")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"memory summary: {exc}")

        if errors and "cpu" not in entry and "memory" not in entry:
            out[device] = {"error": "; ".join(errors)}
        else:
            if errors:
                entry["error"] = "; ".join(errors)
            out[device] = entry
    return out


def parse_processes_cpu(text: str) -> dict[str, Any] | None:
    """Parse XR ``show processes cpu`` utilization header (ignore PID table)."""
    if not text:
        return None
    match = _CPU_UTIL.search(text)
    if not match:
        return None
    node_match = _CPU_NODE.search(text)
    return {
        "one_min": int(match.group("one")),
        "five_min": int(match.group("five")),
        "fifteen_min": int(match.group("fifteen")),
        "node": node_match.group("node") if node_match else None,
    }


def parse_memory_summary(text: str) -> dict[str, Any] | None:
    """Parse XR ``show memory summary`` physical memory line."""
    if not text:
        return None
    match = _PHYS_MEM.search(text)
    if not match:
        return None
    total_mb = int(match.group("total"))
    available_mb = int(match.group("available"))
    used_pct = None
    if total_mb > 0:
        used_pct = round(100.0 * (total_mb - available_mb) / total_mb, 1)
    return {
        "total_mb": total_mb,
        "available_mb": available_mb,
        "used_pct": used_pct,
    }


def _exec_show_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    if result.get("status") == "error":
        return ""
    data = unwrap_mcp_data(result)
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        return str(data.get("result") or "")
    return ""
