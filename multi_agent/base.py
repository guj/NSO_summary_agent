"""Allowlists and plan gating shared by the investigation planners."""

from __future__ import annotations

import json
import re
from typing import Any

ISIS_ALLOWLIST = frozenset({"exec_show"})
BGP_ALLOWLIST = frozenset({"exec_show", "verify_bgp_peer_reachability"})
DEVICE_ALLOWLIST = frozenset(
    {"exec_show", "get_hardware_health", "get_interface_health"}
)

_MAX_TASKS_DEFAULT = 5

_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")

# exec_show prepends "show "; these are not show commands.
_EXEC_SHOW_FORBIDDEN_PREFIXES = (
    "ping",
    "traceroute",
    "tracert",
    "attach",
    "run ",
    "bash",
)


def _exec_show_command_forbidden(cmd: str) -> bool:
    text = (cmd or "").strip().lower()
    if not text:
        return False
    for prefix in _EXEC_SHOW_FORBIDDEN_PREFIXES:
        if text == prefix.rstrip() or text.startswith(prefix):
            return True
    return False


def _service_pair_present(args: dict[str, Any]) -> bool:
    st = args.get("service_type") or args.get("type")
    sn = (
        args.get("service_name")
        or args.get("name")
        or args.get("service_id")
        or args.get("id")
    )
    return bool(
        isinstance(st, str)
        and st.strip()
        and isinstance(sn, str)
        and sn.strip()
    )


def task_rejection_reason(
    check: str,
    args: dict[str, Any],
    *,
    allowlist: frozenset[str],
    device_names: set[str],
    force_device: str | None = None,
) -> str | None:
    """Return why a deep-check task is rejected, or None if acceptable."""
    if not isinstance(check, str) or check not in allowlist:
        return f"tool {check!r} not on allowlist {sorted(allowlist)}"

    device = args.get("device") or args.get("device_name")
    if force_device:
        device = force_device
    if device is not None:
        if not isinstance(device, str) or not device.strip():
            return "device_name must be a non-empty string"
        if device not in device_names:
            return f"device {device!r} not in known_devices"
        try:
            from nso_facts.mcp_client import is_device_quarantined

            if is_device_quarantined(device):
                return (
                    f"device {device!r} is quarantined this run "
                    "(live MCP unavailable — do not retry; use another endpoint "
                    "or conclude with uncertainty)"
                )
        except Exception:  # noqa: BLE001 — quarantine optional outside MCP ctx
            pass

    if check == "exec_show":
        cmd = args.get("command") or args.get("input_command")
        if not isinstance(cmd, str) or not cmd.strip():
            return "exec_show requires input_command (CLI without leading 'show')"
        if _exec_show_command_forbidden(cmd):
            return (
                f"exec_show cannot run {cmd!r} (becomes 'show …'). "
                "Ping/traceroute are not show commands — omit reachability via "
                "exec_show; do not retry."
            )
        if not (force_device or device):
            return "exec_show requires device_name"

    if check in ("get_hardware_health", "get_interface_health"):
        if not (force_device or device):
            return f"{check} requires device_name"

    if check in ("check_service_sync", "compare_service_config"):
        if not _service_pair_present(args):
            return (
                f"{check} requires service_type + service_name "
                "(never device_name alone)"
            )

    return None


def dedupe_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop exact duplicate issues (same code/message/edge_id), keep order."""
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for issue in issues:
        key = (
            issue.get("code"),
            issue.get("message"),
            issue.get("edge_id"),
            issue.get("layer"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(issue)
    return out


def parse_plan_json(text: str) -> list[dict[str, Any]]:
    """Extract a JSON task array from model output."""
    raw = (text or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = _JSON_ARRAY_RE.search(raw)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def gate_plan(
    tasks: list[dict[str, Any]],
    *,
    allowlist: frozenset[str],
    device_names: set[str],
    max_tasks: int = _MAX_TASKS_DEFAULT,
    force_device: str | None = None,
) -> list[dict[str, Any]]:
    """Keep only schema-valid, allowlisted tasks (budget capped).

    If ``force_device`` is set, every task is pinned to that device (wrong
    device names are rewritten or rejected for tools that need a device).
    Rejects quarantined devices, ping/traceroute via exec_show, and
    check_service_sync without service_type + service_name.
    """
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in tasks:
        check = task.get("check")
        if not isinstance(check, str) or check not in allowlist:
            continue
        args = task.get("args")
        if not isinstance(args, dict):
            args = {}
        else:
            args = dict(args)

        if force_device:
            args["device"] = force_device
            if "device_name" in args:
                args["device_name"] = force_device

        if task_rejection_reason(
            check,
            args,
            allowlist=allowlist,
            device_names=device_names,
            force_device=force_device,
        ):
            continue

        if force_device and check in (
            "get_hardware_health",
            "get_interface_health",
        ):
            args = {"device_name": force_device}

        key = json.dumps({"check": check, "args": args}, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        accepted.append(
            {
                "check": check,
                "args": args,
                "reason": task.get("reason"),
                "issue_id": task.get("issue_id") or task.get("edge_id"),
            }
        )
        if len(accepted) >= max_tasks:
            break
    return accepted


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
