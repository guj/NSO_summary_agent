"""FastMCP client helpers for the Cisco NSO MCP server."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Protocol

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

# Named payload keys from the MCP unified-envelope contract.
_WALK_PAYLOAD_KEYS = ("config", "payload", "state", "platform")

# Transient transport failures only — MCP status:error envelopes are not retried.
_MCP_ATTEMPTS = 3
_MCP_BACKOFF_SECONDS = (0.5, 1.0)
_RETRYABLE_EXC = (TimeoutError, ConnectionError, OSError)


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


def mcp_transport(settings: McpSettings) -> StdioTransport:
    return StdioTransport(
        command=settings.mcp_server_cmd,
        args=settings.mcp_server_args,
        env=settings.mcp_env,
        keep_alive=False,
    )


@asynccontextmanager
async def mcp_session(settings: McpSettings) -> AsyncIterator[Client]:
    async with Client(mcp_transport(settings)) as client:
        yield client


async def call_mcp(
    client: Client,
    tool: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """Call an MCP tool once, with retries on transient transport failures.

    Retries up to 2 times (3 attempts) with 0.5s then 1.0s backoff on
    ``TimeoutError`` / ``ConnectionError`` / ``OSError`` only. MCP
    ``status: error`` responses are returned as-is (not retried).
    """
    payload = {"params": params or {}}
    last_exc: BaseException | None = None
    for attempt in range(_MCP_ATTEMPTS):
        try:
            result = await client.call_tool(tool, payload)
            return _tool_data(result)
        except _RETRYABLE_EXC as exc:
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
    raise last_exc
