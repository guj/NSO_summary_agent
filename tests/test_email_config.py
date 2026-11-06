"""Tests for email configuration validation."""

from pathlib import Path

import pytest

from agent.config import Settings, validate_email_settings


def _settings(**overrides) -> Settings:
    defaults = {
        "mcp_server_cmd": "x",
        "mcp_server_args": [],
        "mcp_env": {},
        "fabric_api_key": "k",
        "fabric_api_url": "https://example.com",
        "fabric_model": "m",
        "state_dir": Path("/tmp"),
        "dry_run": False,
        "ignore_service_types": frozenset(),
        "max_service_types": 10,
        "slack_webhook_url": None,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_user": None,
        "smtp_password": None,
        "smtp_use_tls": True,
        "email_from": "summarybot@fabric.com",
        "email_to": ["ops@example.com"],
        "email_subject_prefix": "NSO Summary",
        "prometheus_pushgateway_url": None,
        "prometheus_job": "nso-summary",
        "prometheus_instance": "default",
        "prompts_dir": Path("/tmp"),
        "interface_equivalences_file": None,
        "iface_troubleshoot_max_tool_rounds": 10,
        "iface_troubleshoot_disable": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_validate_email_settings_ok_when_email_disabled():
    assert validate_email_settings(_settings(email_to=[])) == []


def test_validate_email_settings_ok_for_complete_config():
    assert validate_email_settings(_settings()) == []


def test_validate_email_settings_requires_smtp_host():
    with pytest.raises(ValueError, match="SMTP_HOST"):
        validate_email_settings(_settings(smtp_host=None))


def test_validate_email_settings_requires_email_from():
    with pytest.raises(ValueError, match="EMAIL_FROM"):
        validate_email_settings(_settings(email_from=None))


def test_validate_email_settings_rejects_invalid_recipient():
    with pytest.raises(ValueError, match="EMAIL_TO"):
        validate_email_settings(_settings(email_to=["not-an-email"]))


def test_validate_email_settings_warns_on_user_without_password():
    warnings = validate_email_settings(
        _settings(smtp_user="relay@example.com", smtp_password=None)
    )
    assert any("SMTP_USER" in warning for warning in warnings)


def test_validate_email_settings_rejects_password_without_user():
    with pytest.raises(ValueError, match="SMTP_PASSWORD"):
        validate_email_settings(_settings(smtp_user=None, smtp_password="secret"))


def test_recipients_may_be_separated_by_commas_or_semicolons():
    from agent.config import _parse_email_list

    assert _parse_email_list("a@example.org, b@example.org; c@example.org;") == [
        "a@example.org", "b@example.org", "c@example.org"]


def test_email_subjects_are_named_after_the_diagnostic_agent_by_default(monkeypatch):
    from agent.config import load_settings

    monkeypatch.setenv("NSO_ADDRESS", "localhost")
    monkeypatch.setenv("NSO_PASSWORD", "test")
    monkeypatch.setenv("FABRIC_AI_API_KEY", "test")
    monkeypatch.delenv("EMAIL_SUBJECT_PREFIX", raising=False)

    assert load_settings().email_subject_prefix == "NSO Diagnostic"
