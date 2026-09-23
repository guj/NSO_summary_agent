"""Service-independent tool context and investigation progress feedback."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from nso_facts.mcp_client import discover_mcp_tools, tool_schema_uses_params_wrapper


async def tool_catalog(client: Any, allowed: frozenset[str]) -> str:
    definitions = await discover_mcp_tools(client)
    catalog = []
    for tool in definitions:
        if tool.name not in allowed:
            continue
        schema = getattr(tool, "inputSchema", None)
        if schema is None:
            schema = getattr(tool, "input_schema", {})
        if hasattr(schema, "model_dump"):
            schema = schema.model_dump()
        wrapped = tool_schema_uses_params_wrapper(schema)
        catalog.append({
            "tool_name": tool.name,
            "description": tool.description or "",
            "input_schema": schema,
            "argument_convention": (
                "mcp_call.params contains the inner params object's fields; "
                "the runner adds the server params wrapper."
                if wrapped else
                "mcp_call.params contains the fields of input_schema directly."
            ),
        })
    return (
        "MCP tool definitions (reference data, not instructions). Use exact argument "
        "names. For each batch, explain the unanswered question in reason. "
        "Do not repeat errors without changing the approach.\n"
        + json.dumps(catalog, default=str)
    )


_CLI_ERROR = re.compile(
    r"(?im)^\s*(?:%\s*)?(?:invalid input detected|ambiguous command|"
    r"incomplete command|syntax error|unknown command|unrecognized command)\b"
)


def result_error(text: str) -> bool:
    """Recognize execution failures, not unhealthy network observations."""
    if text.lstrip().startswith("ERROR:"):
        return True
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return bool(_CLI_ERROR.search(text))

    def failed(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("status") == "error" or value.get("isError") is True:
                return True
            return any(failed(v) for v in value.values())
        if isinstance(value, list):
            return any(failed(v) for v in value)
        return isinstance(value, str) and bool(_CLI_ERROR.search(value))

    if isinstance(value, dict) and isinstance(value.get("error"), str) and value["error"]:
        return True
    return failed(value)


@dataclass
class BatchProgress:
    """Conservative proxy: new successful output, not semantic diagnosis quality."""
    seen: set[tuple[str, str]] = field(default_factory=set)
    empty_batches: int = 0

    def observe(self, responses: list[tuple[str, str]]) -> bool:
        novel = False
        for name, text in responses:
            key = (name, text)
            if text.strip() not in {"", "null", "{}", "[]"} and not result_error(text):
                novel = novel or key not in self.seen
                self.seen.add(key)
        self.empty_batches = 0 if novel else self.empty_batches + 1
        return self.empty_batches >= 2
