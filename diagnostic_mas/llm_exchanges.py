"""Each LLM investigation's conversation, kept with the run so a later prompt can be replayed against it.

The conversation is what the model was shown and answered: the request, each tool call it made and the reply it
got back (as the model saw it, after any clipping), and its conclusion. The system prompt is kept once per distinct
text. The file is local state like ``case.json``; it is not published.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

FORMAT_VERSION = 1
FILE_NAME = "llm_exchanges.json"
# One investigation's messages, in characters of JSON; a longer one is cut and marked.
MAX_EXCHANGE_CHARS = 400_000
_CUT = "\n[... cut: the saved conversation reached its size limit ...]"


def _size(message: dict[str, Any]) -> int:
    return len(json.dumps(message, default=str))


def _within(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """The messages up to the size limit, the one that crosses it cut; and whether any was cut."""
    kept: list[dict[str, Any]] = []
    room = MAX_EXCHANGE_CHARS
    for message in messages:
        size = _size(message)
        if size <= room:
            kept.append(message)
            room -= size
            continue
        text = message.get("content")
        if isinstance(text, str) and room > 0:
            kept.append({**message, "content": text[:room] + _CUT})
        return kept, True
    return kept, False


def record_exchange(case: Any, *, kind: str, subject: dict[str, Any], messages: list[dict[str, Any]],
                    model: str | None, outcome: dict[str, Any]) -> None:
    """Add one investigation's conversation to the case, to be saved with the run."""
    if not messages:
        return
    system = ""
    body = list(messages)
    if body and body[0].get("role") == "system":
        system, body = str(body[0].get("content") or ""), body[1:]
    key = hashlib.sha256(system.encode("utf-8")).hexdigest()[:16]
    case.llm_texts.setdefault(key, system)
    kept, truncated = _within([dict(m) for m in body])
    case.llm_exchanges.append({
        "kind": kind, "subject": subject, "model": model, "system": key, "messages": kept,
        "truncated": truncated, "outcome": outcome,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    })


def exchanges_document(case: Any) -> dict[str, Any] | None:
    """What is saved as ``llm_exchanges.json``, or None when the scan had no LLM investigation."""
    if not getattr(case, "llm_exchanges", None):
        return None
    return {"format_version": FORMAT_VERSION, "texts": dict(case.llm_texts), "exchanges": list(case.llm_exchanges)}
