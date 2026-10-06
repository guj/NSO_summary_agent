"""LLM client helpers for the OpenAI-compatible chat API."""

from __future__ import annotations

from typing import Any

from openai import OpenAI

from agent.config import Settings, load_settings

# Bound Fabric/NRP chat calls. OpenAI SDK default is 600s and routinely
# sits 2–3+ minutes per plan turn with no progress logs.
FABRIC_CHAT_TIMEOUT_SEC = 60.0
# Default: one attempt, no automatic retries. Settings may override via env.
FABRIC_CHAT_MAX_RETRIES = 0


def llm_timeout_seconds(settings) -> float:
    return float(getattr(settings, "fabric_chat_timeout_sec", FABRIC_CHAT_TIMEOUT_SEC))


def llm_connect_timeout_seconds(settings) -> float:
    return float(getattr(settings, "fabric_chat_connect_timeout_sec", 20.0))


def llm_error_category(error: BaseException) -> str:
    """Classify the SDK cause chain, never guess from elapsed time."""
    import httpx
    seen = set()
    current = error
    chain = []
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    for cls, label in ((httpx.ConnectTimeout, "connection_timeout"),
                       (httpx.ReadTimeout, "response_read_timeout"),
                       (httpx.WriteTimeout, "request_write_timeout"),
                       (httpx.PoolTimeout, "connection_pool_timeout"),
                       (httpx.ConnectError, "connection_error")):
        if any(isinstance(exc, cls) for exc in chain):
            return label
    from openai import APITimeoutError, APIConnectionError
    if any(isinstance(exc, (APITimeoutError, TimeoutError, httpx.TimeoutException)) for exc in chain):
        return "timeout_unspecified"
    if isinstance(error, APIConnectionError):
        return "connection_error"
    return type(error).__name__


def fabric_openai_client(
    settings: Settings | None = None,
    *,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> OpenAI:
    """OpenAI-compatible client for FABRIC AI / substitute providers.

    ``FABRIC_AI_API_URL`` must be an OpenAI chat base (host root or ``…/v1``).
    Anthropic-only bases (e.g. ``…/anthropic``) are not supported.

    ``timeout`` sets read/write/pool inactivity limits, not total wall-clock time. Defaults to
    the settings value from ``FABRIC_CHAT_TIMEOUT_SEC`` (default 60s). ``max_retries`` defaults to
    the settings value from ``FABRIC_CHAT_MAX_RETRIES`` (default 0).
    Connection establishment uses FABRIC_CHAT_CONNECT_TIMEOUT_SEC (default 20s).
    Retries apply to all SDK-retryable errors, including timeouts.
    """
    s = settings or load_settings()
    base = s.fabric_api_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    import httpx

    bound = llm_timeout_seconds(s) if timeout is None else float(timeout)
    retries = (
        getattr(s, "fabric_max_retries", FABRIC_CHAT_MAX_RETRIES)
        if max_retries is None else int(max_retries)
    )
    kwargs: dict[str, Any] = {
        "api_key": s.fabric_api_key,
        "base_url": base,
        "timeout": httpx.Timeout(bound, connect=llm_connect_timeout_seconds(s)),
        "max_retries": retries,
    }
    return OpenAI(**kwargs)
