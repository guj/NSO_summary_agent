"""FABRIC AI API key lifetime lookup and startup reporting."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from agent.config import Settings

_SKIP_LLM_BANNER = """\
========================================================================
WARNING: FABRIC AI API KEY {reason}
LLM calls will be SKIPPED for this run (deterministic collect/spine only).
Renew at https://cm.fabric-testbed.net and update FABRIC_AI_API_KEY in .env
========================================================================
"""


@dataclass(frozen=True)
class FabricKeyLifetime:
    expires_at: datetime | None
    source: str
    detail: str | None = None
    auth_invalid: bool = False
    spend: float | None = None
    max_budget: float | None = None
    budget_reset_at: datetime | None = None

    @property
    def days_remaining(self) -> int | None:
        if self.expires_at is None:
            return None
        return (self.expires_at.date() - datetime.now(UTC).date()).days

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at

    @property
    def budget_remaining(self) -> float | None:
        if self.spend is None or self.max_budget is None:
            return None
        return float(self.max_budget) - float(self.spend)

    @property
    def budget_exhausted(self) -> bool:
        rem = self.budget_remaining
        return rem is not None and rem <= 0

    @property
    def should_skip_llm(self) -> bool:
        """True for known-expired, auth-invalid, or exhausted spend budget."""
        return self.auth_invalid or self.is_expired or self.budget_exhausted

    def format_line(self) -> str:
        if self.auth_invalid:
            detail = self.detail or "unauthorized"
            return f"FABRIC AI API key lifetime: invalid/revoked ({detail})"

        if self.expires_at is None:
            hint = (
                "set FABRIC_AI_KEY_EXPIRES or FABRIC_AI_KEY_CREATED in .env, "
                "or ensure /key/info is reachable"
            )
            return f"FABRIC AI API key lifetime: unknown ({hint})"

        expires = self.expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        days = self.days_remaining
        if days is None:
            suffix = ""
        elif self.is_expired:
            suffix = f" — expired {abs(days)} day(s) ago" if days < 0 else " — expired"
        elif days == 0:
            suffix = " — expires today"
        elif days == 1:
            suffix = " — 1 day remaining"
        else:
            suffix = f" — {days} days remaining"

        line = f"FABRIC AI API key lifetime: expires {expires}{suffix}"
        if self.detail:
            line = f"{line} ({self.detail})"
        return line

    def format_allowance_line(self) -> str | None:
        """Spend/budget from ``/key/info``, or None when unavailable."""
        if self.spend is None and self.max_budget is None:
            return None
        bits: list[str] = []
        if self.spend is not None:
            bits.append(f"spend={self.spend:.4g}")
        if self.max_budget is not None:
            bits.append(f"max_budget={self.max_budget:.4g}")
        rem = self.budget_remaining
        if rem is not None:
            bits.append(f"{rem:.4g} remaining")
        if self.budget_reset_at is not None:
            reset = self.budget_reset_at.astimezone(UTC).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            bits.append(f"resets {reset}")
        line = "FABRIC AI API allowance: " + "; ".join(bits)
        if self.detail:
            line = f"{line} ({self.detail})"
        return line


def print_fabric_api_key_lifetime(settings: Settings) -> FabricKeyLifetime:
    """Print key expiry to stderr; return parsed lifetime info."""
    lifetime, _ = prepare_fabric_llm(settings)
    return lifetime


def prepare_fabric_llm(settings: Settings) -> tuple[FabricKeyLifetime, bool]:
    """Print lifetime / resolved model / warnings.

    Returns ``(lifetime, force_skip_llm)``.

    ``force_skip_llm`` is True when the key is known-expired,
    ``/key/info`` returns 401/403, or spend already exceeds max_budget.
    Unknown expiry does not force skip.
    """
    lifetime = get_fabric_api_key_lifetime(settings)
    print(lifetime.format_line(), file=sys.stderr)
    allowance = lifetime.format_allowance_line()
    if allowance:
        print(allowance, file=sys.stderr)

    if lifetime.auth_invalid:
        print(
            _SKIP_LLM_BANNER.format(
                reason="INVALID OR REVOKED (HTTP 401/403 from /key/info)"
            ),
            file=sys.stderr,
        )
        return lifetime, True

    if lifetime.is_expired:
        print(
            _SKIP_LLM_BANNER.format(reason="EXPIRED"),
            file=sys.stderr,
        )
        return lifetime, True

    if lifetime.budget_exhausted:
        rem = lifetime.budget_remaining
        spend = lifetime.spend
        budget = lifetime.max_budget
        print(
            _SKIP_LLM_BANNER.format(
                reason=(
                    f"SPEND BUDGET EXCEEDED "
                    f"(spend={spend}, max_budget={budget}, remaining={rem})"
                )
            ),
            file=sys.stderr,
        )
        return lifetime, True

    print(format_fabric_model_line(resolve_fabric_model(settings)), file=sys.stderr)

    days = lifetime.days_remaining
    if days is not None and days <= 7:
        print(
            f"warning: FABRIC AI API key expires in {days} day(s) — renew soon",
            file=sys.stderr,
        )
    rem = lifetime.budget_remaining
    if rem is not None and rem <= 5:
        print(
            f"warning: FABRIC AI API allowance low "
            f"({rem:.4g} remaining of max_budget={lifetime.max_budget})",
            file=sys.stderr,
        )
    return lifetime, False


@dataclass(frozen=True)
class FabricModelResolve:
    requested: str
    resolved: str | None
    detail: str | None = None

    def format_line(self) -> str:
        return format_fabric_model_line(self)


def format_fabric_model_line(info: FabricModelResolve) -> str:
    if info.resolved:
        line = (
            f"FABRIC AI LLM model: requested={info.requested} "
            f"resolved={info.resolved}"
        )
        if info.detail:
            line = f"{line} ({info.detail})"
        return line
    hint = info.detail or "probe failed"
    return (
        f"FABRIC AI LLM model: requested={info.requested} "
        f"resolved=unknown ({hint})"
    )


def resolve_fabric_model(settings: Settings) -> FabricModelResolve:
    """Probe chat completions; return gateway ``model`` after nearest-match."""
    requested = str(settings.fabric_model or "").strip() or "gpt-oss-20b"
    base = _chat_api_base(settings.fabric_api_url)
    url = f"{base}/chat/completions"
    body = json.dumps(
        {
            "model": requested,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {settings.fabric_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = f"HTTP {exc.code}"
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:200]
            if err_body:
                detail = f"{detail}: {err_body}"
        except Exception:  # noqa: BLE001
            pass
        return FabricModelResolve(requested=requested, resolved=None, detail=detail)
    except urllib.error.URLError as exc:
        return FabricModelResolve(
            requested=requested,
            resolved=None,
            detail=f"unreachable ({exc.reason})",
        )
    except (json.JSONDecodeError, TimeoutError, OSError) as exc:
        return FabricModelResolve(
            requested=requested,
            resolved=None,
            detail=f"error ({exc})",
        )

    resolved = payload.get("model") if isinstance(payload, dict) else None
    if isinstance(resolved, str) and resolved.strip():
        return FabricModelResolve(
            requested=requested,
            resolved=resolved.strip(),
            detail="chat/completions",
        )
    return FabricModelResolve(
        requested=requested,
        resolved=None,
        detail="no model field in chat/completions response",
    )


def _chat_api_base(fabric_api_url: str) -> str:
    base = fabric_api_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def get_fabric_api_key_lifetime(settings: Settings) -> FabricKeyLifetime:
    remote = _fetch_key_lifetime_from_api(settings)
    if remote.auth_invalid:
        return remote
    has_allowance = remote.spend is not None or remote.max_budget is not None
    if remote.expires_at is not None:
        return remote
    env_lifetime = _key_lifetime_from_env()
    if env_lifetime.expires_at is not None:
        if has_allowance:
            return FabricKeyLifetime(
                expires_at=env_lifetime.expires_at,
                source=env_lifetime.source,
                detail=env_lifetime.detail,
                spend=remote.spend,
                max_budget=remote.max_budget,
                budget_reset_at=remote.budget_reset_at,
            )
        return env_lifetime
    if has_allowance:
        return remote
    if remote.detail and remote.detail != "unavailable":
        return FabricKeyLifetime(
            expires_at=None,
            source="unavailable",
            detail=remote.detail,
        )
    return FabricKeyLifetime(expires_at=None, source="unknown")


def _fetch_key_lifetime_from_api(settings: Settings) -> FabricKeyLifetime:
    base = settings.fabric_api_url.rstrip("/")
    auth_seen = False
    for path in ("/key/info", "/v1/key/info"):
        url = f"{base}{path}"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {settings.fabric_api_key}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                auth_seen = True
                continue
            if exc.code == 404:
                continue
            return FabricKeyLifetime(
                expires_at=None,
                source="api",
                detail=f"{path} HTTP {exc.code}",
            )
        except urllib.error.URLError as exc:
            return FabricKeyLifetime(
                expires_at=None,
                source="api",
                detail=f"{path} unreachable ({exc.reason})",
            )
        except (json.JSONDecodeError, TimeoutError, OSError) as exc:
            return FabricKeyLifetime(
                expires_at=None,
                source="api",
                detail=f"{path} error ({exc})",
            )

        expires = _extract_expires(payload)
        spend, max_budget, reset_at = _extract_allowance(payload)
        if expires is None and spend is None and max_budget is None:
            continue
        return FabricKeyLifetime(
            expires_at=expires,
            source="api",
            detail=path,
            spend=spend,
            max_budget=max_budget,
            budget_reset_at=reset_at,
        )

    if auth_seen:
        return FabricKeyLifetime(
            expires_at=None,
            source="api",
            detail="HTTP 401/403",
            auth_invalid=True,
        )
    return FabricKeyLifetime(expires_at=None, source="api", detail="unavailable")


def _key_lifetime_from_env() -> FabricKeyLifetime:
    explicit = os.environ.get("FABRIC_AI_KEY_EXPIRES", "").strip()
    if explicit:
        expires = _parse_datetime(explicit)
        if expires is not None:
            return FabricKeyLifetime(
                expires_at=expires,
                source="env",
                detail="FABRIC_AI_KEY_EXPIRES",
            )

    created_raw = os.environ.get("FABRIC_AI_KEY_CREATED", "").strip()
    if not created_raw:
        return FabricKeyLifetime(expires_at=None, source="env")

    created = _parse_datetime(created_raw)
    if created is None:
        return FabricKeyLifetime(
            expires_at=None,
            source="env",
            detail="invalid FABRIC_AI_KEY_CREATED",
        )

    lifetime_days = int(os.environ.get("FABRIC_AI_KEY_LIFETIME_DAYS", "30"))
    expires = created + timedelta(days=lifetime_days)
    return FabricKeyLifetime(
        expires_at=expires,
        source="env",
        detail=f"FABRIC_AI_KEY_CREATED + {lifetime_days}d",
    )


def _extract_expires(payload: dict[str, Any]) -> datetime | None:
    candidates: list[Any] = []
    for path in (
        ("info", "expires"),
        ("expires",),
        ("data", "expires"),
        ("key_info", "expires"),
    ):
        node: Any = payload
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if node is not None:
            candidates.append(node)

    for value in candidates:
        parsed = _parse_datetime(str(value))
        if parsed is not None:
            return parsed
    return None


def _extract_allowance(
    payload: dict[str, Any],
) -> tuple[float | None, float | None, datetime | None]:
    """Return ``(spend, max_budget, budget_reset_at)`` from ``/key/info``."""
    roots: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        roots.append(payload)
        for key in ("info", "data", "key_info"):
            node = payload.get(key)
            if isinstance(node, dict):
                roots.append(node)

    spend: float | None = None
    max_budget: float | None = None
    reset_at: datetime | None = None
    for root in roots:
        if spend is None:
            spend = _as_float(root.get("spend"))
        if max_budget is None:
            max_budget = _as_float(root.get("max_budget"))
            if max_budget is None:
                max_budget = _as_float(root.get("soft_budget"))
        if reset_at is None:
            raw = root.get("budget_reset_at")
            if raw is not None:
                reset_at = _parse_datetime(str(raw))
    return spend, max_budget, reset_at


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%B %d %Y", "%b %d %Y"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
