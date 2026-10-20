"""What a scan used of the LLM.

Token counts come with every answer and work with any OpenAI-compatible
endpoint. Spend and budget are read from the provider when it reports them, as
a LiteLLM proxy does at ``/key/info`` and ``/user/info``; with any other
provider they are simply absent. Nothing here can stop a scan.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

_lock = threading.Lock()
_ZERO = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "uncounted": 0}
_totals = dict(_ZERO)
_baseline: "LlmSpend | None" = None


# --- token counts ---------------------------------------------------------------------

def reset_llm_usage() -> None:
    with _lock:
        _totals.update(_ZERO)


def _field(node: Any, name: str) -> Any:
    return node.get(name) if isinstance(node, dict) else getattr(node, name, None)


def record_llm_usage(response: Any) -> None:
    """Add one answer's token counts; an answer without them still counts as a request."""
    usage = _field(response, "usage")
    sent, received = _field(usage, "prompt_tokens"), _field(usage, "completion_tokens")
    counted = isinstance(sent, int) and isinstance(received, int)
    with _lock:
        _totals["requests"] += 1
        if counted:
            _totals["input_tokens"] += sent
            _totals["output_tokens"] += received
        else:
            _totals["uncounted"] += 1


def llm_usage_totals() -> dict[str, int]:
    with _lock:
        return dict(_totals)


def count_usage(client: Any) -> Any:
    """Make an OpenAI-style client record the token counts of every answer it returns."""
    completions = client.chat.completions
    create = completions.create

    def counted(*args: Any, **kwargs: Any) -> Any:
        response = create(*args, **kwargs)
        record_llm_usage(response)
        return response

    counted.counts_usage = True  # type: ignore[attr-defined]
    completions.create = counted
    return client


# --- spend and budget, where the provider reports them --------------------------------

@dataclass(frozen=True)
class LlmSpend:
    """One reading: the key's running total, and the budget it is counted against (the key's own, else its user's)."""
    key_spend: float | None = None
    budget_spend: float | None = None
    budget_max: float | None = None
    budget_period: str | None = None
    budget_reset_at: datetime | None = None

    @property
    def budget_left(self) -> float | None:
        if self.budget_spend is None or self.budget_max is None:
            return None
        return self.budget_max - self.budget_spend


def provider_root(api_url: str) -> str:
    """The provider's address without the chat API's ``/v1``: where a proxy serves its own information."""
    return re.sub(r"/v1$", "", str(api_url or "").rstrip("/"))


def _get_json(url: str, key: str) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _allowance(node: Any) -> tuple[float | None, float | None, datetime | None, str | None]:
    from agent.fabric_key import _extract_allowance

    if not isinstance(node, dict):
        return None, None, None, None
    spend, limit, reset_at = _extract_allowance(node)
    inner = node.get("info") if isinstance(node.get("info"), dict) else node
    period = inner.get("budget_duration")
    return spend, limit, reset_at, str(period) if period else None


def read_llm_spend(settings: Any, *, get: Callable[[str, str], Any] | None = None) -> LlmSpend | None:
    """The provider's own figures now; None when it reports none."""
    get = get or _get_json
    root, key = provider_root(settings.fabric_api_url), settings.fabric_api_key
    key_spend, key_max, key_reset, key_period = _allowance(get(f"{root}/key/info", key))
    user = get(f"{root}/user/info", key)
    user_spend, user_max, user_reset, user_period = _allowance((user or {}).get("user_info") if isinstance(user, dict) else None)
    if key_max is not None:
        return LlmSpend(key_spend, key_spend, key_max, key_period, key_reset)
    if user_max is not None:
        return LlmSpend(key_spend, user_spend, user_max, user_period, user_reset)
    if key_spend is None and user_spend is None:
        return None
    return LlmSpend(key_spend=key_spend, budget_spend=None if key_spend is not None else user_spend)


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _budget_words(spend: float, limit: float, period: str | None, left: float, reset: str | None) -> str:
    return (f"{_money(spend)} of {_money(limit)} used" + (f" ({period})" if period else "")
            + f", {_money(left)} left" + (f", resets {reset}" if reset else ""))


def budget_line(spend: LlmSpend | None) -> str:
    """One line for the start of a scan."""
    if spend is None:
        return "LLM spend: this provider does not report spend or budget."
    if spend.budget_left is not None:
        reset = spend.budget_reset_at.date().isoformat() if spend.budget_reset_at else None
        return "LLM budget: " + _budget_words(spend.budget_spend, spend.budget_max, spend.budget_period,
                                              spend.budget_left, reset) + "."
    total = spend.key_spend if spend.key_spend is not None else spend.budget_spend
    return f"LLM spend on this key so far: {_money(total)}. No budget is set."


def usage_record(before: LlmSpend | None, after: LlmSpend | None, totals: dict[str, int]) -> dict[str, Any]:
    """What is kept with the scan: the token counts, and the cost and budget when the provider gave them."""
    record: dict[str, Any] = dict(totals) | {"cost": None}
    pairs = [("key", before and before.key_spend, after and after.key_spend),
             ("account", before and before.budget_spend, after and after.budget_spend)]
    for basis, start, end in pairs:
        # A total that went back was reset between the readings; that says nothing about this scan.
        if start is not None and end is not None and end >= start:
            record |= {"cost": round(end - start, 2), "spend_before": start, "spend_after": end}
            if basis == "account":
                record["cost_basis"] = "account"
            break
    # A reading that failed is not a provider without spend figures: say which one is missing.
    if record["cost"] is None and (before is None) != (after is None):
        record["missing_reading"] = "end" if after is None else "start"
        if before is not None and before.key_spend is not None:
            record["spend_before"] = before.key_spend
    budget = after if after is not None else before
    if budget is not None and budget.budget_left is not None:
        record |= {"budget_spend": budget.budget_spend, "budget_max": budget.budget_max,
                   "budget_left": round(budget.budget_left, 5), "budget_period": budget.budget_period,
                   "budget_reset_at": budget.budget_reset_at.date().isoformat() if budget.budget_reset_at else None}
        if budget is before:
            record["budget_read_at"] = "start"
    return record


def usage_lines(record: dict[str, Any] | None) -> list[str]:
    """The run-details line for this scan; none for a scan that did not use the LLM.

    Only this scan's cost and tokens: the budget is the line at the start of the scan, and the
    provider's running totals stay in the record, because readers took them for this scan's figures.
    """
    if not record:
        return []
    tokens = (f"{record.get('requests', 0):,} requests · {record.get('input_tokens', 0):,} input and "
              f"{record.get('output_tokens', 0):,} output tokens")
    missing = int(record.get("uncounted") or 0)
    if missing:
        tokens += f" · {missing:,} answer{'' if missing == 1 else 's'} gave no token counts"
    if record.get("cost") is not None:
        basis = " (from the account's spend)" if record.get("cost_basis") == "account" else ""
        return [f"**LLM this scan:** {_money(record['cost'])}{basis} · {tokens}. "
                "The last request may not be counted yet."]
    missing_reading = record.get("missing_reading")
    if missing_reading in {"start", "end"}:
        why = f"cost unknown: the provider gave no spend reading at the {missing_reading} of the scan"
    else:
        why = "no spend reading from the provider"
    return [f"**LLM this scan:** {tokens}; {why}."]


def begin_llm_accounting(settings: Any, *, get: Callable[[str, str], Any] | None = None) -> str:
    """Start a scan's count and take the provider's reading; returns the line to show."""
    global _baseline
    reset_llm_usage()
    _baseline = read_llm_spend(settings, get=get)
    return budget_line(_baseline)


def end_llm_accounting(settings: Any, *, get: Callable[[str, str], Any] | None = None) -> dict[str, Any]:
    """Take the provider's reading again and return the scan's record."""
    return usage_record(_baseline, read_llm_spend(settings, get=get), llm_usage_totals())
