"""FastMCP client helpers for the Cisco NSO MCP server."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Protocol

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from nso_facts.mcp_accounting import record_mcp_call
from nso_facts.mcp_archive import record_mcp_result

# Named payload keys from the MCP unified-envelope contract.
_WALK_PAYLOAD_KEYS = ("config", "payload", "state", "platform")

# Transient transport failures only — MCP status:error envelopes are not retried.
_MCP_ATTEMPTS = 3
_MCP_BACKOFF_SECONDS = (0.5, 1.0)
_RETRYABLE_EXC = (TimeoutError, ConnectionError, OSError)

# Run-scoped response cache (active inside ``mcp_session`` only).
_mcp_cache: ContextVar[dict[str, Any] | None] = ContextVar(
    "mcp_response_cache", default=None
)
# tool_name -> True if inputSchema expects a top-level ``params`` object.
_mcp_tool_wraps_params: ContextVar[dict[str, bool] | None] = ContextVar(
    "mcp_tool_wraps_params", default=None
)


_mcp_tool_definitions: ContextVar[list[Any] | None] = ContextVar(
    "mcp_tool_definitions", default=None
)

# device_name -> first unreachable reason (run-scoped; with mcp cache lifetime).
_mcp_quarantine: ContextVar[dict[str, str] | None] = ContextVar(
    "mcp_device_quarantine", default=None
)


async def discover_mcp_tools(client: Client) -> list[Any]:
    """Discover once per run; retain descriptions and schemas for LLM callers."""
    cached = _mcp_tool_definitions.get()
    if cached is not None:
        return cached
    definitions = list(await client.list_tools())
    _mcp_tool_definitions.set(definitions)
    return definitions


class McpSettings(Protocol):
    mcp_server_cmd: str
    mcp_server_args: list[str]
    mcp_env: dict[str, str]


def _tool_text(result: Any) -> str:
    if result is None:
        return ""
    content = getattr(result, "content", None)
    if not content:
        return str(result)
    first = content[0]
    return getattr(first, "text", str(first))


def _tool_data(result: Any) -> Any:
    data = getattr(result, "data", None)
    if data is not None:
        return data

    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        if set(structured.keys()) == {"result"}:
            return structured["result"]
        return structured

    text = _tool_text(result).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def mcp_is_error(result: Any) -> bool:
    return isinstance(result, dict) and result.get("status") == "error"


def mcp_error_message(result: Any) -> str | None:
    if not mcp_is_error(result):
        return None
    return str(result.get("error_message") or "unknown error")


def mcp_data(result: Any) -> dict[str, Any]:
    """Return the success ``data`` object, or ``{}`` on error / non-envelope."""
    if not isinstance(result, dict):
        return {}
    if result.get("status") == "error":
        return {}
    if result.get("status") == "success":
        data = result.get("data")
        return data if isinstance(data, dict) else {}
    return result


def mcp_walk_root(result: Any) -> Any:
    """Root object for YANG / config deep-walks.

    Peels the MCP success envelope, then prefers named payload keys
    (``config``, ``payload``, ``state``, ``platform``). Legacy responses
    with YANG directly under ``data`` are returned as-is. Truncation
    markers (no payload) are preserved so callers can detect them.
    """
    if mcp_is_error(result):
        return {}
    if not isinstance(result, dict):
        return {}

    data = mcp_data(result) if result.get("status") == "success" else result
    if not isinstance(data, dict):
        return {}

    for key in _WALK_PAYLOAD_KEYS:
        nested = data.get(key)
        if isinstance(nested, (dict, list)):
            return nested
    return data


def unwrap_mcp_data(result: Any) -> dict[str, Any]:
    """Dict form of :func:`mcp_walk_root` for topology parsers."""
    root = mcp_walk_root(result)
    return root if isinstance(root, dict) else {}


def unwrap_mcp_result_text(value: Any) -> str:
    """Extract human-readable tool body (CLI text or pretty JSON).

    Unwraps common MCP envelopes (``status``/``data``/``result``/``output``/…)
    and normalizes newlines. Does not clip — callers apply their own limits.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            value = str(value)
    if isinstance(value, str):
        text = value.replace("\r\n", "\n").replace("\r", "\n")
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return unwrap_mcp_result_text(json.loads(stripped))
            except json.JSONDecodeError:
                return text
        return text
    if isinstance(value, dict):
        # Legacy deep_checks truncation — preview is already lossy.
        if value.get("truncated") is True and isinstance(value.get("preview"), str):
            return unwrap_mcp_result_text(value["preview"])
        if value.get("status") == "error":
            err = (
                value.get("error_message")
                or value.get("error")
                or value.get("message")
            )
            if err is not None:
                return unwrap_mcp_result_text(err)
        for key in (
            "result",
            "output",
            "text",
            "stdout",
            "config",
            "payload",
            "state",
            "platform",
        ):
            if key in value and value[key] is not None:
                return unwrap_mcp_result_text(value[key])
        if "data" in value and value["data"] is not None:
            return unwrap_mcp_result_text(value["data"])
        return json.dumps(value, indent=2, default=str, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), indent=2, default=str, ensure_ascii=False)
    return str(value)


def mcp_transport(settings: McpSettings) -> StdioTransport:
    return StdioTransport(
        command=settings.mcp_server_cmd,
        args=settings.mcp_server_args,
        env=settings.mcp_env,
        keep_alive=False,
    )


def _cache_key(tool: str, params: dict[str, Any] | None) -> str:
    body = json.dumps(params or {}, sort_keys=True, default=str, separators=(",", ":"))
    return f"{tool}\0{body}"


def _device_name_from_params(params: dict[str, Any] | None) -> str | None:
    if not isinstance(params, dict):
        return None
    name = params.get("device_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _looks_live_unreachable(message: str) -> bool:
    """True when a live MCP failure should quarantine further device calls."""
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "timed out",
            "timeout",
            "unreachable",
            "connection refused",
            "connect timeout",
            "read timed out",
        )
    )


def is_strong_device_unreachability(message: str) -> bool:
    """True only with explicit unreachability — not NSO HTTP/read timeouts.

    HTTPSConnectionPool / Read timed out against the NSO API means the live
    query timed out; it does not prove the network device itself is down.
    """
    lowered = (message or "").lower()
    if not lowered:
        return False
    # REST/HTTP timeouts to NSO are not device-reachability proof.
    if any(
        token in lowered
        for token in (
            "httpsconnectionpool",
            "read timed out",
            "read timeout",
        )
    ):
        return "unreachable" in lowered and "httpsconnectionpool" not in lowered
    return "unreachable" in lowered


def live_mcp_failure_kind(message: str) -> str:
    """Classify a quarantine/live-MCP failure: unreachable | timeout | other."""
    if is_strong_device_unreachability(message):
        return "unreachable"
    lowered = (message or "").lower()
    if any(
        token in lowered
        for token in ("timed out", "timeout", "read timed out", "connect timeout")
    ):
        return "timeout"
    return "other"


_READ_TIMEOUT_RE = re.compile(
    r"read\s+timeout\s*=\s*(\d+(?:\.\d+)?)", re.IGNORECASE
)
_TOOL_PREFIX_RE = re.compile(r"^([A-Za-z_][\w.-]*)\s*:\s+")


def parse_live_mcp_read_timeout_sec(message: str) -> str | None:
    """Return the read-timeout seconds from a quarantine/error string, if any."""
    match = _READ_TIMEOUT_RE.search(message or "")
    if not match:
        return None
    raw = match.group(1)
    try:
        val = float(raw)
    except ValueError:
        return raw
    if val == int(val):
        return str(int(val))
    return raw


def parse_live_mcp_failed_operation(message: str) -> str:
    """Best-effort name of the failing automated collection operation."""
    raw = (message or "").strip()
    # Preferred: "exec_show (isis adjacency): …"
    paren = re.match(
        r"^([A-Za-z_][\w.-]*)\s*\(([^)]+)\)\s*:",
        raw,
    )
    if paren:
        return f"{paren.group(1)} ({paren.group(2).strip()})"
    match = _TOOL_PREFIX_RE.match(raw)
    if match:
        return match.group(1)
    lowered = raw.lower()
    if "httpsconnectionpool" in lowered or "restconf" in lowered:
        return "NSO RESTCONF live-MCP"
    if "live query" in lowered or "timed out" in lowered or "timeout" in lowered:
        return "NSO live-MCP collection"
    return "NSO live-MCP collection"


def parse_live_mcp_collection_phase(message: str) -> str:
    """Human phase label for follow-ups (e.g. IS-IS collection)."""
    raw = (message or "").strip()
    lower = raw.lower()
    cmd = ""
    paren = re.search(r"exec_show\s*\(([^)]+)\)", lower)
    if paren:
        cmd = paren.group(1)
    blob = f"{cmd} {lower}"
    if re.search(r"\bisis\b", blob):
        return "IS-IS collection"
    if re.search(r"\bbgp\b", blob):
        return "BGP collection"
    if re.search(r"\b(l2vpn|xconnect|evpn|bridge-domain)\b", blob):
        return "L2VPN collection"
    if "check_device_sync" in blob or re.search(r"\bsync\b", blob):
        return "device-sync collection"
    if "get_interface_health" in blob or "interfaces" in blob:
        return "interface-health collection"
    if "get_hardware_health" in blob or "hardware" in blob:
        return "hardware-health collection"
    if "exec_show" in blob:
        return "live exec_show collection"
    return "live MCP collection"


def format_live_mcp_quarantine_prose(
    device: str, reason: str, *, for_issue: bool = True
) -> str:
    """Operator-facing quarantine text; prefer timeout wording over unreachable."""
    kind = live_mcp_failure_kind(reason)
    detail = (reason or "").strip()
    if kind == "unreachable":
        head = f"{device}: live unreachable this run"
        action = "further live MCP to this device was skipped for the rest of the run"
    elif kind == "timeout":
        op = parse_live_mcp_failed_operation(detail)
        timeout = parse_live_mcp_read_timeout_sec(detail)
        timeout_bit = f" (read timeout={timeout}s)" if timeout else ""
        head = (
            f"{device}: automated NSO live-MCP collection timed out on "
            f"{op}{timeout_bit}"
        )
        action = (
            "manual NSO connectivity may still succeed — this does not prove "
            "the device is down; investigate the automated collection path; "
            "further live MCP to this device was skipped for the rest of the run"
        )
    else:
        head = f"{device}: live query failed this run"
        action = "further live MCP to this device was skipped for the rest of the run"
    if detail and kind != "timeout":
        head = f"{head} ({detail})"
    elif detail and kind == "timeout":
        # Keep the raw error available without implying device-down.
        head = f"{head} [{detail}]"
    if for_issue:
        return f"{head}. NSO still has inventory/config; {action}."
    return head


def quarantined_devices() -> dict[str, str]:
    """Return ``{device_name: reason}`` for devices skipped this run."""
    q = _mcp_quarantine.get()
    return dict(q) if q else {}


def is_device_quarantined(device_name: str) -> bool:
    q = _mcp_quarantine.get()
    return bool(q and device_name in q)


def _quarantine_device(device_name: str, reason: str) -> None:
    q = _mcp_quarantine.get()
    if q is None:
        return
    if device_name in q:
        return
    q[device_name] = reason
    print(
        f"[mcp] quarantine device={device_name} "
        f"(further live MCP skipped this run): {reason}",
        file=sys.stderr,
    )


def _quarantine_skip_result(device_name: str, reason: str) -> dict[str, str]:
    kind = live_mcp_failure_kind(reason)
    if kind == "unreachable":
        cause = "live unreachable"
    elif kind == "timeout":
        cause = "live query timed out"
    else:
        cause = "live query failure"
    return {
        "status": "error",
        "error_message": (
            f"device {device_name} quarantined this run after {cause} "
            f"({reason}); further live MCP skipped — NSO config already "
            f"fetched may still be used from cache"
        ),
    }


def _maybe_quarantine_from_failure(
    params: dict[str, Any] | None,
    message: str,
    *,
    tool: str | None = None,
) -> None:
    device = _device_name_from_params(params)
    if not device or not _looks_live_unreachable(message):
        return
    err = (message or "").strip() or "live query failure"
    tool_name = str(tool or "").strip()
    cmd = ""
    if isinstance(params, dict) and tool_name == "exec_show":
        cmd = str(
            params.get("input_command") or params.get("command") or ""
        ).strip()
    if tool_name and cmd:
        # Avoid double-prefix if the error already includes the tool label.
        if err.lower().startswith(f"{tool_name.lower()} ("):
            detail = err
        elif err.lower().startswith(f"{tool_name.lower()}:"):
            detail = f"{tool_name} ({cmd}): {err.split(':', 1)[1].strip()}"
        else:
            detail = f"{tool_name} ({cmd}): {err}"
    elif tool_name and not err.lower().startswith(f"{tool_name.lower()}:"):
        detail = f"{tool_name}: {err}"
    else:
        detail = err
    _quarantine_device(device, detail)


def tool_schema_uses_params_wrapper(schema: Any) -> bool:
    """True when the tool's inputSchema has a top-level ``params`` property.

    Legacy NSO MCP tools used ``{"params": {...}}``. Newer servers expose flat
    properties (``device_name``, ``input_command``, …) at the top level.
    """
    if schema is None:
        return False
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump()
    elif hasattr(schema, "dict") and not isinstance(schema, dict):
        try:
            schema = schema.dict()
        except Exception:  # noqa: BLE001
            return False
    if not isinstance(schema, dict):
        return False
    props = schema.get("properties")
    if not isinstance(props, dict):
        return False
    return "params" in props


def build_mcp_payload(
    params: dict[str, Any] | None,
    *,
    wrap_params: bool,
) -> dict[str, Any]:
    """Build ``call_tool`` arguments for wrapped or flat tool schemas."""
    body = dict(params or {})
    if wrap_params:
        return {"params": body}
    return body


async def _tool_wraps_params(client: Client, tool: str) -> bool:
    """Resolve whether ``tool`` expects the legacy ``params`` wrapper."""
    cache = _mcp_tool_wraps_params.get()
    if cache is not None and tool in cache:
        return cache[tool]

    try:
        tools = await discover_mcp_tools(client)
    except Exception as exc:  # noqa: BLE001
        print(
            f"warning: MCP list_tools failed ({exc}); "
            f"calling {tool!r} with flat arguments",
            file=sys.stderr,
        )
        tools = []

    discovered: dict[str, bool] = {}
    for t in tools:
        name = getattr(t, "name", None)
        if not isinstance(name, str) or not name:
            continue
        schema = getattr(t, "inputSchema", None)
        if schema is None:
            schema = getattr(t, "input_schema", None)
        discovered[name] = tool_schema_uses_params_wrapper(schema)

    if cache is None:
        cache = {}
        _mcp_tool_wraps_params.set(cache)
    cache.update(discovered)
    if tool in cache:
        return cache[tool]
    # Unknown tool / empty list_tools: prefer flat (current server shape).
    cache[tool] = False
    return False


def start_mcp_cache() -> None:
    """Begin run-scoped caching of successful MCP responses."""
    _mcp_cache.set({})
    _mcp_tool_wraps_params.set({})
    _mcp_tool_definitions.set(None)
    _mcp_quarantine.set({})


def successful_live_devices() -> list[str]:
    """Devices with a successful live query in this session, not CDB inventory."""
    live_tools = {"exec_show", "check_isis_adjacencies", "get_interface_health",
                  "get_hardware_health"}
    devices: set[str] = set()
    for key, result in (_mcp_cache.get() or {}).items():
        tool, body = key.split("\0", 1)
        if tool not in live_tools or not isinstance(result, dict):
            continue
        if result.get("status") != "success":
            continue
        device = _device_name_from_params(json.loads(body))
        if device and not is_device_quarantined(device):
            devices.add(device)
    return sorted(devices)


def stop_mcp_cache() -> None:
    """Drop the run-scoped cache."""
    _mcp_cache.set(None)
    _mcp_tool_wraps_params.set(None)
    _mcp_tool_definitions.set(None)
    _mcp_quarantine.set(None)


@asynccontextmanager
async def mcp_session(settings: McpSettings) -> AsyncIterator[Client]:
    start_mcp_cache()
    try:
        async with Client(mcp_transport(settings)) as client:
            yield client
    finally:
        stop_mcp_cache()


async def call_mcp(
    client: Client,
    tool: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """Call an MCP tool once, with retries on transient transport failures.

    Retries up to 2 times (3 attempts) with 0.5s then 1.0s backoff on
    ``TimeoutError`` / ``ConnectionError`` / ``OSError`` only. MCP
    ``status: error`` responses are returned as-is (not retried).

    Argument shape follows ``list_tools()``: if the tool schema declares a
    top-level ``params`` property, send ``{"params": {...}}``; otherwise send
    flat arguments (current NSO MCP server).

    Inside ``mcp_session``, identical ``(tool, params)`` reuse the first
    successful response (errors are not cached). After a device-scoped live
    unreachable / timeout failure, further wire calls for that ``device_name``
    are skipped for the rest of the run (cached successes still apply).
    """
    cache = _mcp_cache.get()
    key = _cache_key(tool, params)
    if cache is not None and key in cache:
        record_mcp_call(tool, params, cached=True)
        record_mcp_result(tool, params, source="cache", response=cache[key])
        return cache[key]

    device = _device_name_from_params(params)
    quarantine = _mcp_quarantine.get()
    if device and quarantine and device in quarantine:
        print(
            f"[mcp] skip tool={tool} device={device} (quarantined)",
            file=sys.stderr,
        )
        skipped = _quarantine_skip_result(device, quarantine[device])
        record_mcp_result(tool, params, source="quarantine_skip", response=skipped)
        return skipped

    wrap = await _tool_wraps_params(client, tool)
    payload = build_mcp_payload(params, wrap_params=wrap)
    record_mcp_call(tool, params, cached=False)
    last_exc: BaseException | None = None
    for attempt in range(_MCP_ATTEMPTS):
        try:
            result = await client.call_tool(tool, payload)
            data = _tool_data(result)
            record_mcp_result(tool, params, source="wire", response=data,
                              raw_response=result, attempt=attempt + 1)
            if mcp_is_error(data):
                _maybe_quarantine_from_failure(
                    params,
                    str(mcp_error_message(data) or ""),
                    tool=tool,
                )
            elif cache is not None:
                cache[key] = data
            return data
        except _RETRYABLE_EXC as exc:
            record_mcp_result(tool, params, source="transport_error",
                              error=str(exc), attempt=attempt + 1)
            last_exc = exc
            if attempt + 1 >= _MCP_ATTEMPTS:
                break
            delay = _MCP_BACKOFF_SECONDS[
                min(attempt, len(_MCP_BACKOFF_SECONDS) - 1)
            ]
            print(
                f"warning: MCP tool {tool!r} failed ({exc}); "
                f"retrying in {delay}s "
                f"(attempt {attempt + 1}/{_MCP_ATTEMPTS})",
                file=sys.stderr,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    _maybe_quarantine_from_failure(params, str(last_exc), tool=tool)
    raise last_exc
