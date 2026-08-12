"""Execute gated deep-check plans via MCP."""

from __future__ import annotations

from typing import Any

from nso_facts.mcp_client import call_mcp


async def run_deep_checks(
    client: Any,
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for task in plan:
        check = str(task.get("check") or "")
        args = dict(task.get("args") or {})
        # Normalize exec_show arg names used by this MCP server
        if check == "exec_show":
            args = _normalize_exec_show_args(args)
        elif check in (
            "get_hardware_health",
            "get_interface_health",
            "verify_bgp_peer_reachability",
        ):
            # MCP tools require device_name; plans often use "device"
            args = _with_device_name(args)
        try:
            result = await call_mcp(client, check, args)
            # MCP may return a validation string instead of raising
            if _is_exec_show_validation_error(check, result):
                evidence.append(
                    {
                        "check": check,
                        "args": args,
                        "issue_id": task.get("issue_id"),
                        "reason": task.get("reason"),
                        "error": _clip_result_message(result),
                    }
                )
            else:
                evidence.append(
                    {
                        "check": check,
                        "args": args,
                        "issue_id": task.get("issue_id"),
                        "reason": task.get("reason"),
                        "result": _truncate(result),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            evidence.append(
                {
                    "check": check,
                    "args": args,
                    "issue_id": task.get("issue_id"),
                    "reason": task.get("reason"),
                    "error": str(exc),
                }
            )
    return evidence


def _normalize_exec_show_args(args: dict[str, Any]) -> dict[str, Any]:
    """Map plan args → MCP exec_show params; strip leading ``show ``."""
    cmd = args.get("input_command") or args.get("command") or ""
    if isinstance(cmd, str):
        cmd = cmd.strip()
        if cmd.lower().startswith("show "):
            cmd = cmd[5:].lstrip()
    out: dict[str, Any] = {
        "device_name": args.get("device") or args.get("device_name"),
        "input_command": cmd,
    }
    return out


def _is_exec_show_validation_error(check: str, result: Any) -> bool:
    if check != "exec_show":
        return False
    msg = _clip_result_message(result).lower()
    return "must not start with 'show" in msg or "must not start with \"show" in msg


def _clip_result_message(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("error_message", "message", "result", "output", "text"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                return val
        return str(result)
    return str(result)


def _with_device_name(args: dict[str, Any]) -> dict[str, Any]:
    out = dict(args)
    dev = out.get("device_name") or out.get("device")
    if dev is not None:
        out["device_name"] = dev
        out.pop("device", None)
    return out


def _truncate(result: Any, limit: int = 4000) -> Any:
    if isinstance(result, str) and len(result) > limit:
        return result[:limit] + "…"
    if isinstance(result, dict):
        text = str(result)
        if len(text) > limit:
            return {"truncated": True, "preview": text[:limit] + "…"}
    return result
