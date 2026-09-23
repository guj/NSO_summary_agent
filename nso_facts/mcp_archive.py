"""Opt-in run-scoped archive of MCP results before consumer truncation."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_archive: ContextVar[Any] = ContextVar("mcp_result_archive", default=None)


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return vars(value)
    return str(value)


@contextmanager
def archive_mcp_results(directory: Path | None, run_id: str):
    """Create a private, unique JSONL file; default mode performs no writes."""
    if directory is None:
        token = _archive.set(None)
        try:
            yield None
        finally:
            _archive.reset(token)
        return
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f"{run_id}-", suffix=".jsonl", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        token = _archive.set(stream)
        try:
            yield Path(name)
        finally:
            _archive.reset(token)


def record_mcp_result(tool: str, params: Any, *, source: str,
                      response: Any = None, raw_response: Any = None,
                      error: str | None = None, attempt: int | None = None) -> None:
    stream = _archive.get()
    if stream is None:
        return
    row = {"timestamp": datetime.now(timezone.utc).isoformat(),
           "tool": tool, "params": params or {}, "source": source,
           "response": response, "raw_response": raw_response,
           "error": error, "attempt": attempt}
    try:
        stream.write(json.dumps(row, default=_json_default, ensure_ascii=False) + "\n")
        stream.flush()
    except (OSError, TypeError, ValueError) as exc:
        # Never retry a network call merely because local archiving failed.
        print(f"[mcp archive] FAILED to save {tool}: {exc}", file=sys.stderr)
