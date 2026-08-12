"""Tool-using FABRIC loop for one interface per device (Devices Observations)."""

from __future__ import annotations

import json
import re
from typing import Any

from agent.config import Settings
from agent.mcp_client import call_mcp

DENIED_MCP_TOOLS = frozenset(
    {
        "sync_from_device",
        "exec_ping",
        "exec_traceroute",
        "verify_dns_reachability",
        "verify_bgp_peer_reachability",
        "verify_ntp_reachability",
        "verify_collector_reachability",
    }
)

MCP_CALL_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_call",
        "description": (
            "Call a read-only NSO MCP tool on the current device. "
            "Use tool_name=exec_show with params.input_command for show CLI "
            "(do not include the word 'show'; MCP prepends it)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "params": {"type": "object"},
            },
            "required": ["tool_name"],
        },
    },
}

_ERROR_KEYS = (
    "input_errors",
    "output_errors",
    "crc",
    "crc_errors",
    "errors",
    "error_count",
    "total_errors",
)


def is_tool_allowed(name: str) -> bool:
    return name not in DENIED_MCP_TOOLS


def select_troubleshoot_target(
    device: str,
    physical_edges: list,
    error_counts: dict[str, int],
) -> tuple[str, str] | None:
    """Return ``(interface, reason)`` or None. reason: up_down | errors."""
    up_down: list[str] = []
    for edge in physical_edges:
        if not isinstance(edge, dict):
            continue
        local = edge.get("local") or {}
        if local.get("device") != device:
            continue
        iface = str(local.get("interface") or "").strip()
        if not iface:
            continue
        if _edge_is_up_down(edge):
            up_down.append(iface)
    if up_down:
        return sorted(up_down)[0], "up_down"

    positive = {k: int(v) for k, v in error_counts.items() if int(v) > 0}
    if not positive:
        return None
    worst_iface = max(positive.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return worst_iface, "errors"


def error_counts_from_interface_health(mcp_result: Any) -> dict[str, int]:
    """Best-effort map iface → error count from get_interface_health payload."""
    root = _unwrap_payload(mcp_result)
    counts: dict[str, int] = {}

    for iface_list in _find_iface_dicts(root):
        for item in iface_list:
            if not isinstance(item, dict):
                continue
            name = (
                item.get("name")
                or item.get("interface")
                or item.get("intf")
                or item.get("ifname")
            )
            if not name:
                continue
            total = _sum_error_fields(item)
            if total > 0:
                counts[str(name)] = max(counts.get(str(name), 0), total)

    # Flagged / problem lists: treat presence as at least 1 if no numeric fields.
    for key in ("flagged", "problems", "interfaces_with_errors", "unhealthy"):
        block = root.get(key) if isinstance(root, dict) else None
        if isinstance(block, list):
            for item in block:
                if isinstance(item, str) and item.strip():
                    counts.setdefault(item.strip(), 1)
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("interface")
                    if name:
                        total = _sum_error_fields(item)
                        counts[str(name)] = max(
                            counts.get(str(name), 0), total if total > 0 else 1
                        )
    return counts


async def execute_troubleshoot_tool(
    client: Any,
    device: str,
    name: str,
    arguments: dict[str, Any] | None,
) -> str:
    if not is_tool_allowed(name):
        return f"ERROR: tool {name!r} denied for troubleshooting"
    raw = arguments or {}
    if isinstance(raw.get("params"), dict):
        params = dict(raw["params"])
    else:
        params = {k: v for k, v in raw.items() if k != "tool_name"}
    params["device_name"] = device
    try:
        result = await call_mcp(client, name, params)
        text = json.dumps(result, default=str)
        return text[:50000]
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"


async def troubleshoot_interface(
    client: Any,
    settings: Settings,
    *,
    device: str,
    interface: str,
    reason: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run FABRIC tool loop; return snapshot entry for one interface."""
    from agent.summarize import fabric_openai_client

    tools_used: list[str] = []
    system = _load_troubleshoot_prompt(settings)
    user_payload = {
        "device": device,
        "interface": interface,
        "reason": reason,
        "context": context or {},
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                "Troubleshoot this interface. Use mcp_call as needed, then finish "
                "with the JSON summary object.\n\n"
                f"{json.dumps(user_payload, indent=2, default=str)}"
            ),
        },
    ]

    oai = fabric_openai_client(settings)
    max_rounds = settings.iface_troubleshoot_max_tool_rounds
    summary: str | None = None
    detail: str | None = None

    try:
        for _ in range(max_rounds):
            response = oai.chat.completions.create(
                model=settings.fabric_model,
                messages=messages,
                tools=[MCP_CALL_TOOL],
                tool_choice="auto",
                temperature=0,
            )
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                messages.append(_message_to_dict(message))
                for call in tool_calls:
                    fn = call.function
                    fn_name = getattr(fn, "name", "") or ""
                    raw_args = getattr(fn, "arguments", "") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except json.JSONDecodeError:
                        args = {}
                    if fn_name == "mcp_call":
                        tool_name = str(args.get("tool_name") or "")
                        tool_params = args.get("params") if isinstance(args.get("params"), dict) else {}
                        label = f"{tool_name}:{json.dumps(tool_params, sort_keys=True)[:80]}"
                        tools_used.append(label)
                        result_text = await execute_troubleshoot_tool(
                            client, device, tool_name, {"params": tool_params}
                        )
                    else:
                        tools_used.append(fn_name)
                        result_text = f"ERROR: unknown tool {fn_name!r}"
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": result_text,
                        }
                    )
                continue

            content = (message.content or "").strip()
            parsed = _parse_summary_json(content)
            summary = parsed.get("summary") or content or "No summary returned."
            detail = parsed.get("detail")
            break
        else:
            if not summary:
                summary = "Troubleshooting incomplete (tool cap)."
    except Exception as exc:  # noqa: BLE001
        summary = f"Troubleshooting unavailable: {exc}"[:160]

    entry: dict[str, Any] = {
        "interface": interface,
        "reason": reason,
        "summary": _clip_summary(summary or "Troubleshooting unavailable."),
        "tools_used": tools_used,
    }
    if detail:
        entry["detail"] = str(detail)
    return entry


async def collect_interface_troubleshooting(
    client: Any,
    settings: Settings,
    topology: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Select targets, run sessions, write ``operational.interface_troubleshooting``."""
    if settings.iface_troubleshoot_disable:
        return {}
    if "devices" not in settings.report_sections:
        return {}

    operational = topology.setdefault("operational", {})
    if not isinstance(operational, dict):
        return {}

    edges = _physical_edges_merged(topology)
    devices = sorted(
        {
            str((e.get("local") or {}).get("device"))
            for e in edges
            if isinstance(e, dict) and (e.get("local") or {}).get("device")
        }
    )
    results: dict[str, dict[str, Any]] = {}

    for device in devices:
        try:
            error_counts: dict[str, int] = {}
            try:
                health = await call_mcp(
                    client, "get_interface_health", {"device_name": device}
                )
                error_counts = error_counts_from_interface_health(health)
            except Exception:  # noqa: BLE001
                error_counts = {}

            selected = select_troubleshoot_target(device, edges, error_counts)
            if not selected:
                continue
            iface, reason = selected
            context = {
                "admin_oper": _admin_oper_for(device, iface, edges),
                "error_count_hint": error_counts.get(iface),
            }
            entry = await troubleshoot_interface(
                client,
                settings,
                device=device,
                interface=iface,
                reason=reason,
                context=context,
            )
            results[device] = entry
        except Exception as exc:  # noqa: BLE001
            results[device] = {
                "interface": "?",
                "reason": "error",
                "summary": f"Troubleshooting unavailable: {exc}"[:160],
                "tools_used": [],
            }

    operational["interface_troubleshooting"] = results
    return results


def _edge_is_up_down(edge: dict[str, Any]) -> bool:
    state = edge.get("state") or {}
    admin = state.get("admin")
    oper = state.get("oper")
    if admin is None or oper is None:
        return False
    return str(admin).lower() == "up" and str(oper).lower() == "down"


def _physical_edges_merged(topology: dict[str, Any]) -> list:
    """Join operational physical edges with static local/remote by id."""
    op = topology.get("operational") if isinstance(topology.get("operational"), dict) else {}
    st = topology.get("static") if isinstance(topology.get("static"), dict) else {}
    op_layers = op.get("layers") if isinstance(op.get("layers"), dict) else {}
    st_layers = st.get("layers") if isinstance(st.get("layers"), dict) else {}
    op_phys = op_layers.get("physical") if isinstance(op_layers.get("physical"), dict) else {}
    st_phys = st_layers.get("physical") if isinstance(st_layers.get("physical"), dict) else {}
    op_edges = op_phys.get("edges") if isinstance(op_phys.get("edges"), list) else []
    st_edges = st_phys.get("edges") if isinstance(st_phys.get("edges"), list) else []

    by_id: dict[str, dict[str, Any]] = {}
    for e in st_edges:
        if isinstance(e, dict) and e.get("id"):
            by_id[str(e["id"])] = dict(e)
    merged: list[dict[str, Any]] = []
    for e in op_edges:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or "")
        base = dict(by_id.get(eid, {}))
        base.update(e)
        if eid and eid in by_id:
            # Prefer static local/remote identity
            if "local" in by_id[eid]:
                base["local"] = by_id[eid]["local"]
            if "remote" in by_id[eid]:
                base["remote"] = by_id[eid].get("remote")
        merged.append(base)
    if not merged and st_edges:
        return [e for e in st_edges if isinstance(e, dict)]
    return merged


def _admin_oper_for(device: str, iface: str, edges: list) -> dict[str, Any]:
    for e in edges:
        if not isinstance(e, dict):
            continue
        local = e.get("local") or {}
        if local.get("device") == device and str(local.get("interface")) == iface:
            state = e.get("state") or {}
            return {
                "admin": state.get("admin"),
                "oper": state.get("oper"),
                "status": state.get("status"),
            }
    return {}


def _load_troubleshoot_prompt(settings: Settings) -> str:
    path = settings.prompts_dir / "interface_troubleshooting_system.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return (
        "Troubleshoot the interface bottom-up OSI. Return JSON "
        '{"summary": "...", "detail": "..."}.'
    )


def _parse_summary_json(content: str) -> dict[str, str]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            out: dict[str, str] = {}
            if data.get("summary") is not None:
                out["summary"] = str(data["summary"])
            if data.get("detail") is not None:
                out["detail"] = str(data["detail"])
            return out
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[^{}]*\"summary\"[^{}]*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict) and data.get("summary") is not None:
                return {
                    "summary": str(data["summary"]),
                    **(
                        {"detail": str(data["detail"])}
                        if data.get("detail") is not None
                        else {}
                    ),
                }
        except json.JSONDecodeError:
            pass
    return {}


def _clip_summary(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _message_to_dict(message: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "role": "assistant",
        "content": message.content,
    }
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        serialized = []
        for call in tool_calls:
            serialized.append(
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
            )
        data["tool_calls"] = serialized
    return data


def _unwrap_payload(mcp_result: Any) -> Any:
    if not isinstance(mcp_result, dict):
        return mcp_result
    if mcp_result.get("status") == "success" and isinstance(mcp_result.get("data"), dict):
        return mcp_result["data"]
    return mcp_result.get("data", mcp_result)


def _find_iface_dicts(root: Any) -> list[list]:
    found: list[list] = []
    if isinstance(root, list):
        if root and isinstance(root[0], dict):
            found.append(root)
        return found
    if not isinstance(root, dict):
        return found
    for key in ("interfaces", "intf", "rows", "items", "summary"):
        val = root.get(key)
        if isinstance(val, list):
            found.append(val)
        elif isinstance(val, dict):
            for nested in ("interfaces", "rows", "items"):
                if isinstance(val.get(nested), list):
                    found.append(val[nested])
    return found


def _sum_error_fields(item: dict[str, Any]) -> int:
    total = 0
    for key in _ERROR_KEYS:
        val = item.get(key)
        if val is None:
            continue
        try:
            total += int(val)
        except (TypeError, ValueError):
            continue
    return total
