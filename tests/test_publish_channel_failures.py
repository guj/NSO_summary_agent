"""Tests that one delivery channel failing does not stop the others or the run."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from agent.publish import publish_all
from test_diagnostic_mas_publish import _settings
from test_diagnostic_skip_llm import _args, _run_offline, llm_requests, nso_env  # noqa: F401

SLACK_REFUSAL = "Slack files.completeUploadExternal failed: not_in_channel"


def _slack_bot_and_email(tmp_path):
    return replace(
        _settings(state_dir=tmp_path),
        slack_webhook_url=None,
        slack_bot_token="xoxb-test",
        slack_channel_id="C123",
        email_to=["ops@example.com"],
        email_from="nso@example.com",
        smtp_host="smtp.example.com",
    )


def _report_file(tmp_path):
    path = tmp_path / "report.html"
    path.write_text("full report")
    return path


def test_email_is_still_sent_when_the_slack_upload_fails(tmp_path):
    failures: list[tuple[str, str]] = []
    with patch("agent.publish.publish_slack_file", side_effect=RuntimeError(SLACK_REFUSAL)):
        with patch("agent.publish.publish_email") as email:
            sent = publish_all(
                "Brief", "run", _slack_bot_and_email(tmp_path),
                attachment_path=_report_file(tmp_path), failures=failures,
            )

    assert email.called
    assert sent == ["email"]
    assert failures == [("slack", SLACK_REFUSAL)]


def test_slack_delivery_stands_when_email_fails(tmp_path):
    failures: list[tuple[str, str]] = []
    with patch("agent.publish.publish_slack_file"):
        with patch("agent.publish.publish_email", side_effect=RuntimeError("SMTP unreachable")):
            sent = publish_all(
                "Brief", "run", _slack_bot_and_email(tmp_path),
                attachment_path=_report_file(tmp_path), failures=failures,
            )

    assert sent == ["slack"]
    assert failures == [("email", "SMTP unreachable")]


def test_a_delivery_failure_still_raises_for_callers_that_do_not_collect_failures(tmp_path):
    with patch("agent.publish.publish_slack_file", side_effect=RuntimeError(SLACK_REFUSAL)):
        with patch("agent.publish.publish_email"):
            with pytest.raises(RuntimeError, match="not_in_channel"):
                publish_all(
                    "Brief", "run", _slack_bot_and_email(tmp_path),
                    attachment_path=_report_file(tmp_path),
                )


@pytest.mark.asyncio
async def test_run_saves_reports_and_sends_email_when_slack_delivery_fails(
    nso_env, llm_requests, monkeypatch, tmp_path, capsys
):
    for name, value in {
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL_ID": "C123",
        "EMAIL_TO": "ops@example.com",
        "EMAIL_FROM": "nso@example.com",
        "SMTP_HOST": "smtp.example.com",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("SLACK_WEBHOOK_URL", "SMTP_USER", "SMTP_PASSWORD", "PROMETHEUS_PUSHGATEWAY_URL"):
        monkeypatch.delenv(name, raising=False)
    args = _args(skip_llm=True)
    args.dry_run, args.publish = False, True

    with patch("agent.publish.publish_slack_file", side_effect=RuntimeError(SLACK_REFUSAL)):
        with patch("agent.publish.publish_email") as email:
            code = await _run_offline(args)

    err = capsys.readouterr().err
    assert email.called
    assert list((tmp_path / "diagnostic_mas" / "runs").glob("*/report.html"))
    assert f"Delivery failed: slack — {SLACK_REFUSAL}" in err
    assert "Published to: email" in err
    assert code == 1


@pytest.mark.asyncio
async def test_run_saves_reports_and_uploads_to_slack_when_email_delivery_fails(
    nso_env, llm_requests, monkeypatch, tmp_path, capsys
):
    for name, value in {
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL_ID": "C123",
        "EMAIL_TO": "ops@example.com",
        "EMAIL_FROM": "nso@example.com",
        "SMTP_HOST": "smtp.example.com",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("SLACK_WEBHOOK_URL", "SMTP_USER", "SMTP_PASSWORD", "PROMETHEUS_PUSHGATEWAY_URL"):
        monkeypatch.delenv(name, raising=False)
    args = _args(skip_llm=True)
    args.dry_run, args.publish = False, True

    with patch("agent.publish.publish_slack_file") as slack:
        with patch("agent.publish.publish_email", side_effect=RuntimeError("SMTP login rejected")):
            code = await _run_offline(args)

    err = capsys.readouterr().err
    assert slack.called
    assert list((tmp_path / "diagnostic_mas" / "runs").glob("*/report.html"))
    assert "Delivery failed: email — SMTP login rejected" in err
    assert "Published to: slack" in err
    assert code == 1


@pytest.mark.asyncio
async def test_run_finishes_when_email_settings_are_incomplete(
    nso_env, llm_requests, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("EMAIL_TO", "ops@example.com")
    for name in ("EMAIL_FROM", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SLACK_WEBHOOK_URL",
                 "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID", "PROMETHEUS_PUSHGATEWAY_URL"):
        monkeypatch.delenv(name, raising=False)
    args = _args(skip_llm=True)
    args.dry_run, args.publish = False, True

    code = await _run_offline(args)

    err = capsys.readouterr().err
    assert list((tmp_path / "diagnostic_mas" / "runs").glob("*/report.html"))
    assert "Delivery failed: email — " in err and "SMTP_HOST is required" in err
    assert code == 1
