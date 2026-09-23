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
        # Normalize LLM/plan arg names → current MCP tool schemas (flat keys).
        if check == "exec_show":
            args = _normalize_exec_show_args(args)
        elif check in (
            "get_hardware_health",
            "get_interface_health",
            "verify_bgp_peer_reachability",
            "get_device_config",
            "compare_device_config",
            "check_device_sync",
            "get_device_platform",
            "get_device_state",
            "get_device_xr_variant",
            "check_isis_adjacencies",
        ):
            # Schemas accept device_name only; drop aliases / extras (e.g. interface).
            args = _device_name_only(args)
        elif check in ("check_service_sync", "compare_service_config"):
            args = _normalize_service_pair_args(args)
        elif check == "get_services":
            args = _normalize_get_services_args(args)
        elif check == "explore_nso_path":
            args = _normalize_explore_path_args(args)
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
                        # Full body for gates / artifacts; LLM paths clip separately.
                        "result": _evidence_result(result),
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
    from nso_facts.mcp_client import unwrap_mcp_result_text

    return unwrap_mcp_result_text(result)


def _device_name_only(args: dict[str, Any]) -> dict[str, Any]:
    """Keep only ``device_name`` (accept ``device`` alias); drop extras."""
    dev = args.get("device_name") or args.get("device")
    out: dict[str, Any] = {}
    if dev is not None:
        out["device_name"] = dev
    return out


def _with_device_name(args: dict[str, Any]) -> dict[str, Any]:
    """Deprecated alias for callers/tests; prefer :func:`_device_name_only`."""
    return _device_name_only(args)


def _normalize_service_pair_args(args: dict[str, Any]) -> dict[str, Any]:
    st = args.get("service_type") or args.get("type")
    sn = (
        args.get("service_name")
        or args.get("name")
        or args.get("service_id")
        or args.get("id")
    )
    out: dict[str, Any] = {}
    if st is not None:
        out["service_type"] = st
    if sn is not None:
        out["service_name"] = sn
    return out


def _normalize_get_services_args(args: dict[str, Any]) -> dict[str, Any]:
    st = args.get("service_type") or args.get("type")
    out: dict[str, Any] = {}
    if st is not None:
        out["service_type"] = st
    return out


def _normalize_explore_path_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    path = args.get("path")
    if path is not None:
        out["path"] = path
    if "depth" in args and args.get("depth") is not None:
        out["depth"] = args.get("depth")
    return out


def _evidence_result(result: Any) -> Any:
    """Store complete tool body for gates/artifacts (no clip).

    Unwraps MCP envelopes to readable text for large dumps; keeps small
    structured dict/list payloads intact. LLM/display clipping is separate
    (``clip_dataplane_tool_content`` / ``_clip_tool_content``).
    """
    import json

    from nso_facts.mcp_client import unwrap_mcp_result_text

    body = unwrap_mcp_result_text(result)
    if isinstance(result, (dict, list)):
        try:
            raw = json.dumps(result, default=str)
        except TypeError:
            return body
        # Tiny structured replies stay as objects; large dumps as full text.
        if len(raw) <= 2000 and len(body) <= 2000:
            return result
    return body


def _truncate(result: Any, limit: int = 4000) -> Any:
    """Clip tool payload for display / LLM helpers (not for stored evidence)."""
    import json

    from nso_facts.mcp_client import unwrap_mcp_result_text

    body = unwrap_mcp_result_text(result)
    if len(body) <= limit:
        if isinstance(result, (dict, list)):
            raw = json.dumps(result, default=str)
            if len(raw) <= limit:
                return result
        return body
    keep = max(0, limit - 72)
    return (
        body[:keep].rstrip()
        + f"\n\n[truncated: showing {keep} of {len(body)} chars]"
    )
