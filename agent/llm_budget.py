"""Detect provider LLM spend-budget exhaustion (LiteLLM / FABRIC)."""

from __future__ import annotations

import json
from typing import Any


class ProviderBudgetExceeded(Exception):
    """Provider refused further LLM calls due to spend budget."""

    def __init__(self, message: str, *, detail: str | None = None):
        self.detail = detail or message
        super().__init__(message)


def is_provider_budget_exceeded(exc: BaseException) -> bool:
    """True when the error is a provider spend budget (not rate-limit TPM)."""
    chunks: list[str] = [str(exc)]
    for attr in ("body", "response", "message"):
        raw = getattr(exc, attr, None)
        if raw is None:
            continue
        if isinstance(raw, (bytes, bytearray)):
            try:
                chunks.append(raw.decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                continue
        elif isinstance(raw, dict):
            try:
                chunks.append(json.dumps(raw, default=str))
            except Exception:  # noqa: BLE001
                chunks.append(str(raw))
        else:
            chunks.append(str(raw))
    # OpenAI SDK often nests body under response.json()
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            data = resp.json()
            if isinstance(data, dict):
                chunks.append(json.dumps(data, default=str))
        except Exception:  # noqa: BLE001
            pass

    blob = " ".join(chunks).lower()
    compact = blob.replace(" ", "").replace("_", "")
    if "budget_exceeded" in blob or "budgetexceeded" in compact:
        return True
    if "exceededbudget" in compact:
        return True
    if "over budget" in blob and ("spend=" in blob or "budget=" in blob):
        return True
    return False


def provider_budget_message(exc: BaseException) -> str:
    """Short operator-facing line from a budget_exceeded exception."""
    text = str(exc).strip()
    if len(text) > 240:
        text = text[:237] + "…"
    return text or "provider LLM budget exceeded"


def mark_case_llm_halt(case: Any, reason: str) -> None:
    """Record a run-wide LLM halt; idempotent (first reason wins)."""
    existing = getattr(case, "llm_halt_reason", None)
    if existing:
        return
    case.llm_halt_reason = reason


def case_llm_halted(case: Any) -> bool:
    return bool(getattr(case, "llm_halt_reason", None))
