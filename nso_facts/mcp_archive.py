"""Opt-in run-scoped archive of MCP results before consumer truncation."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_archive: ContextVar[Any] = ContextVar("mcp_result_archive", default=None)
# <run id>-<mkstemp random>.jsonl, as archive_mcp_results names them.
_ARCHIVE_NAME = re.compile(r"(\d{8}T\d{6}Z)-[a-z0-9_]+\.jsonl")
_RUN_ID = "%Y%m%dT%H%M%SZ"


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return vars(value)
    return str(value)


def _run_time(run_id: str) -> datetime | None:
    try:
        return datetime.strptime(run_id, _RUN_ID).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _prune(directory: Path, run_id: str, keep_days: int) -> None:
    """Remove this agent's archives whose run is more than ``keep_days`` before this one.

    Only files directly in ``directory`` named like an archive are candidates, so
    anything moved into a subfolder (e.g. ``keep/``) stays. Age is read from the run
    ID in the name, not the file time, which a copy or restore would change.
    """
    cutoff = (_run_time(run_id) or datetime.now(timezone.utc)) - timedelta(days=keep_days)
    removed = 0
    for path in sorted(directory.iterdir()):
        match = _ARCHIVE_NAME.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file() or _run_time(match[1]) >= cutoff:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            # Retention never stops the scan that is about to save its own results.
            print(f"[mcp archive] could not remove {path.name}: {exc}", file=sys.stderr)
    if removed:
        print(f"[mcp archive] Removed {removed} archive{'s' * (removed != 1)} older than "
              f"{keep_days} days from {directory}", file=sys.stderr)


@contextmanager
def archive_mcp_results(directory: Path | None, run_id: str, *, keep_days: int = 0):
    """Create a private, unique JSONL file; default mode performs no writes.

    With ``keep_days`` > 0, older archives in ``directory`` are removed first;
    0 keeps them all.
    """
    if directory is None:
        token = _archive.set(None)
        try:
            yield None
        finally:
            _archive.reset(token)
        return
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if keep_days > 0:
        _prune(directory, run_id, keep_days)
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
