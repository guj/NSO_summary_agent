"""Tests for FABRIC AI API key lifetime reporting."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from agent.config import Settings
from agent.fabric_key import (
    FabricKeyLifetime,
    FabricModelResolve,
    _extract_expires,
    format_fabric_model_line,
    get_fabric_api_key_lifetime,
    prepare_fabric_llm,
    resolve_fabric_model,
)


def _settings(**overrides) -> Settings:
    base = {
        "mcp_server_cmd": "cmd",
        "mcp_server_args": [],
        "mcp_env": {},
        "fabric_api_key": "sk-test",
        "fabric_api_url": "https://ai.fabric-testbed.net",
        "fabric_model": "gpt-oss-20b",
        "state_dir": "/tmp/state",
        "dry_run": False,
        "ignore_service_types": frozenset(),
        "max_service_types": 10,
        "report_sections": (
            "problems",
            "counts",
            "delta",
            "fleet_sync",
            "devices",
            "ignored_types",
        ),
        "slack_webhook_url": None,
        "smtp_host": None,
        "smtp_port": 587,
        "smtp_user": None,
        "smtp_password": None,
        "smtp_use_tls": True,
        "email_from": None,
        "email_to": [],
        "email_subject_prefix": "NSO Summary",
        "prometheus_pushgateway_url": None,
        "prometheus_job": "nso-summary",
        "prometheus_instance": "default",
        "prompts_dir": "/tmp/prompts",
        "topology_force_update": False,
        "interface_equivalences_file": None,
        "iface_troubleshoot_max_tool_rounds": 10,
        "iface_troubleshoot_disable": False,
    }
    base.update(overrides)
    return Settings(**base)


def test_extract_allowance_and_format_line():
    from agent.fabric_key import FabricKeyLifetime, _extract_allowance

    spend, budget, reset = _extract_allowance(
        {
            "info": {
                "expires": "2027-01-21T00:00:00Z",
                "spend": 42.5,
                "max_budget": 50.0,
                "budget_reset_at": "2026-10-01T00:00:00Z",
            }
        }
    )
    assert spend == 42.5
    assert budget == 50.0
    assert reset is not None
    line = FabricKeyLifetime(
        expires_at=datetime(2027, 1, 21, tzinfo=UTC),
        source="api",
        detail="/key/info",
        spend=spend,
        max_budget=budget,
        budget_reset_at=reset,
    ).format_allowance_line()
    assert line is not None
    assert "spend=42.5" in line
    assert "max_budget=50" in line
    assert "remaining" in line
    assert "7.5 remaining" in line or "7.500" in line


def test_budget_exhausted_skips_llm(monkeypatch):
    from agent.fabric_key import FabricKeyLifetime, prepare_fabric_llm

    monkeypatch.setattr(
        "agent.fabric_key.get_fabric_api_key_lifetime",
        lambda _s: FabricKeyLifetime(
            expires_at=datetime.now(UTC) + timedelta(days=30),
            source="api",
            detail="/key/info",
            spend=50.01,
            max_budget=50.0,
        ),
    )
    lifetime, skip = prepare_fabric_llm(_settings())
    assert lifetime.budget_exhausted
    assert skip is True
    assert lifetime.should_skip_llm


def test_extract_expires_nested_info():
    payload = {"info": {"expires": "2026-07-25T12:00:00Z"}}
    expires = _extract_expires(payload)
    assert expires == datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def test_format_line_days_remaining():
    expires = datetime.now(UTC) + timedelta(days=10)
    line = FabricKeyLifetime(
        expires_at=expires,
        source="env",
        detail="FABRIC_AI_KEY_CREATED + 30d",
    ).format_line()
    assert "10 days remaining" in line
    assert "FABRIC_AI_KEY_CREATED + 30d" in line


def test_env_fallback_created_plus_lifetime(monkeypatch):
    monkeypatch.setenv("FABRIC_AI_KEY_CREATED", "2026-06-25")
    monkeypatch.setenv("FABRIC_AI_KEY_LIFETIME_DAYS", "30")
    # Force API miss so env fallback is exercised (live /key/info would win).
    with patch(
        "agent.fabric_key._fetch_key_lifetime_from_api",
        return_value=FabricKeyLifetime(
            expires_at=None, source="api", detail="unavailable"
        ),
    ):
        lifetime = get_fabric_api_key_lifetime(_settings())
    assert lifetime.source == "env"
    assert lifetime.expires_at == datetime(2026, 7, 25, 0, 0, tzinfo=UTC)


def test_api_preferred_over_env(monkeypatch):
    monkeypatch.setenv("FABRIC_AI_KEY_CREATED", "2026-01-01")
    payload = json.dumps({"expires": "2026-08-01T00:00:00Z"}).encode()

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return payload

    with patch("urllib.request.urlopen", return_value=FakeResp()):
        lifetime = get_fabric_api_key_lifetime(_settings())

    assert lifetime.source == "api"
    assert lifetime.expires_at == datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def test_expired_warning_days_remaining():
    expires = datetime.now(UTC) - timedelta(days=2)
    lifetime = FabricKeyLifetime(expires_at=expires, source="api")
    assert lifetime.days_remaining == -2
    assert lifetime.is_expired
    assert lifetime.should_skip_llm


def test_prepare_fabric_llm_skips_when_expired(capsys):
    expires = datetime.now(UTC) - timedelta(hours=1)
    with patch(
        "agent.fabric_key.get_fabric_api_key_lifetime",
        return_value=FabricKeyLifetime(expires_at=expires, source="api", detail="/key/info"),
    ):
        with patch("agent.fabric_key.resolve_fabric_model") as resolve_mock:
            lifetime, force_skip = prepare_fabric_llm(_settings())
    assert force_skip is True
    assert lifetime.should_skip_llm
    resolve_mock.assert_not_called()
    err = capsys.readouterr().err
    assert "LLM calls will be SKIPPED" in err
    assert "EXPIRED" in err


def test_prepare_fabric_llm_skips_when_auth_invalid(capsys):
    with patch(
        "agent.fabric_key.get_fabric_api_key_lifetime",
        return_value=FabricKeyLifetime(
            expires_at=None,
            source="api",
            detail="HTTP 401/403",
            auth_invalid=True,
        ),
    ):
        with patch("agent.fabric_key.resolve_fabric_model") as resolve_mock:
            lifetime, force_skip = prepare_fabric_llm(_settings())
    assert force_skip is True
    assert lifetime.should_skip_llm
    resolve_mock.assert_not_called()
    err = capsys.readouterr().err
    assert "INVALID OR REVOKED" in err
    assert "LLM calls will be SKIPPED" in err


def test_prepare_fabric_llm_does_not_skip_when_unknown(capsys):
    with patch(
        "agent.fabric_key.get_fabric_api_key_lifetime",
        return_value=FabricKeyLifetime(expires_at=None, source="unknown"),
    ):
        with patch(
            "agent.fabric_key.resolve_fabric_model",
            return_value=FabricModelResolve(
                requested="gpt-oss-20b",
                resolved="gpt-oss-20b",
                detail="chat/completions",
            ),
        ):
            lifetime, force_skip = prepare_fabric_llm(_settings())
    assert force_skip is False
    assert not lifetime.should_skip_llm
    err = capsys.readouterr().err
    assert "LLM calls will be SKIPPED" not in err
    assert "FABRIC AI LLM model: requested=gpt-oss-20b resolved=gpt-oss-20b" in err


def test_format_fabric_model_line_unknown():
    line = format_fabric_model_line(
        FabricModelResolve(
            requested="gpt-oss-20b",
            resolved=None,
            detail="HTTP 404",
        )
    )
    assert "requested=gpt-oss-20b" in line
    assert "resolved=unknown" in line
    assert "HTTP 404" in line


def test_resolve_fabric_model_reads_response_model():
    payload = json.dumps({"id": "chatcmpl-1", "model": "exact-model-id"}).encode()

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return payload

    with patch("urllib.request.urlopen", return_value=FakeResp()):
        info = resolve_fabric_model(_settings(fabric_model="alias-name"))
    assert info.requested == "alias-name"
    assert info.resolved == "exact-model-id"
    assert info.detail == "chat/completions"


def test_api_401_marks_auth_invalid():
    import urllib.error

    def raise_401(*_a, **_k):
        raise urllib.error.HTTPError(
            url="https://example/key/info",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )

    with patch("urllib.request.urlopen", side_effect=raise_401):
        lifetime = get_fabric_api_key_lifetime(_settings())
    assert lifetime.auth_invalid
    assert lifetime.should_skip_llm
