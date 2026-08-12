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
    def should_skip_llm(self) -> bool:
        """True only for known-expired or auth-invalid keys (not unknown)."""
        return self.auth_invalid or self.is_expired

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


def print_fabric_api_key_lifetime(settings: Settings) -> FabricKeyLifetime:
    """Print key expiry to stderr; return parsed lifetime info."""
    lifetime, _ = prepare_fabric_llm(settings)
    return lifetime


def prepare_fabric_llm(settings: Settings) -> tuple[FabricKeyLifetime, bool]:
    """Print lifetime / warnings.

    Returns ``(lifetime, force_skip_llm)``.

    ``force_skip_llm`` is True only when the key is known-expired or
    ``/key/info`` returns 401/403. Unknown expiry does not force skip.
    """
    lifetime = get_fabric_api_key_lifetime(settings)
    print(lifetime.format_line(), file=sys.stderr)

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

    days = lifetime.days_remaining
    if days is not None and days <= 7:
        print(
            f"warning: FABRIC AI API key expires in {days} day(s) — renew soon",
            file=sys.stderr,
        )
    return lifetime, False


def get_fabric_api_key_lifetime(settings: Settings) -> FabricKeyLifetime:
    remote = _fetch_key_lifetime_from_api(settings)
    if remote.auth_invalid:
        return remote
    if remote.expires_at is not None:
        return remote
    env_lifetime = _key_lifetime_from_env()
    if env_lifetime.expires_at is not None:
        return env_lifetime
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
        if expires is None:
            continue
        return FabricKeyLifetime(
            expires_at=expires,
            source="api",
            detail=path,
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
