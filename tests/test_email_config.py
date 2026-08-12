"""Tests for email configuration validation."""

from email.message import EmailMessage
from pathlib import Path

import pytest

from agent.config import Settings, validate_email_settings
from agent.publish import send_test_email


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
        "report_sections": (
            "problems",
            "counts",
            "delta",
            "fleet_sync",
            "devices",
            "ignored_types",
        ),
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
        "topology_force_update": False,
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


def test_send_test_email_sends_message(monkeypatch):
    sent: list[EmailMessage] = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=30):
            self.host = host
            self.port = port

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            return None

        def starttls(self, context=None):
            return None

        def login(self, user, password):
            assert user == "relay@example.com"
            assert password == "secret"

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr("agent.publish.smtplib.SMTP", FakeSMTP)

    send_test_email(
        _settings(
            smtp_user="relay@example.com",
            smtp_password="secret",
        )
    )
    assert len(sent) == 1
    assert sent[0]["To"] == "ops@example.com"
    assert "email configuration test" in sent[0].get_content()
