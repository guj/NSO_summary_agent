"""Tests for markdown → Slack/email channel rendering."""

from __future__ import annotations

import json
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

from agent.markdown_channels import (
    markdown_to_html,
    markdown_to_plain,
    markdown_to_slack,
    outputs_from_markdown,
    slack_payload_from_markdown,
)
from agent.publish import publish_all, publish_email, publish_slack


SAMPLE = """# NSO Diagnostic Report

**Scope:** 2 devices · 1 service
**Result:** No fault identified.

## Devices

### renc-data-sw

**NSO sync:** In sync
**Health:** Interface and hardware checks reported healthy

**Attention:** BGP neighbor `10.148.0.1` could not be mapped.

## Recommended follow-up

1. Resolve mapping observations.
2. Explain health flags.
"""


def test_markdown_to_html_has_structure():
    html = markdown_to_html(SAMPLE)
    assert "<h1>NSO Diagnostic Report</h1>" in html
    assert "<h2>Devices</h2>" in html
    assert "<h3>renc-data-sw</h3>" in html
    assert "<strong>Scope:</strong>" in html or "<strong>Scope:</strong> 2 devices" in html
    assert "<code>10.148.0.1</code>" in html
    assert "<ol>" in html
    assert "<li>Resolve mapping observations.</li>" in html
    assert "<!DOCTYPE html>" in html


def test_markdown_to_slack_uses_mrkdwn():
    text = markdown_to_slack(SAMPLE)
    assert "*NSO Diagnostic Report*" in text
    assert "*Devices*" in text
    assert "*renc-data-sw*" in text
    assert "*Scope:*" in text or "*Scope:* 2 devices" in text
    assert "`10.148.0.1`" in text
    assert "• " not in text or "1. Resolve" in text
    assert "**" not in text  # MD bold converted


def test_markdown_to_plain_strips_markup():
    plain = markdown_to_plain(SAMPLE)
    assert "NSO Diagnostic Report" in plain
    assert "#" not in plain.splitlines()[0]
    assert "**" not in plain
    assert "10.148.0.1" in plain


def test_slack_payload_uses_blocks():
    payload = slack_payload_from_markdown(SAMPLE)
    assert "blocks" in payload
    assert payload["blocks"][0]["type"] == "section"
    assert payload["blocks"][0]["text"]["type"] == "mrkdwn"
    assert "*Devices*" in payload["blocks"][0]["text"]["text"]


def test_outputs_from_markdown_fills_all_channels():
    outs = outputs_from_markdown(SAMPLE)
    assert outs.markdown.startswith("# NSO")
    assert "<h1>" in outs.html
    assert "**" not in outs.plain


def test_publish_slack_posts_blocks():
    settings = MagicMock()
    settings.slack_webhook_url = "https://hooks.example/x"
    captured: dict = {}

    class _Resp:
        def read(self):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=30):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    with patch("agent.publish.urllib.request.urlopen", fake_urlopen):
        publish_slack(SAMPLE, settings)

    assert "blocks" in captured["body"]
    assert captured["body"]["blocks"][0]["text"]["type"] == "mrkdwn"


def test_publish_email_adds_html_alternative():
    settings = MagicMock()
    settings.email_from = "a@example.com"
    settings.email_to = ["b@example.com"]
    settings.email_subject_prefix = "NSO"
    settings.smtp_host = "smtp.example"
    settings.smtp_port = 587
    settings.smtp_use_tls = False
    settings.smtp_user = None
    settings.smtp_password = None

    sent: list[EmailMessage] = []

    def fake_send(s, msg):
        sent.append(msg)

    with patch("agent.publish.email_configured", return_value=True):
        with patch("agent.publish.validate_email_settings"):
            with patch("agent.publish._send_via_smtp", fake_send):
                publish_email(
                    markdown_to_plain(SAMPLE),
                    "run-1",
                    settings,
                    report_html=markdown_to_html(SAMPLE),
                )

    assert len(sent) == 1
    msg = sent[0]
    # multipart: text/plain + text/html
    assert msg.is_multipart() or msg.get_content_type() == "text/plain"
    payloads = list(msg.iter_parts()) if msg.is_multipart() else [msg]
    types = {p.get_content_type() for p in payloads}
    assert "text/html" in types or (
        msg.get_body(preferencelist=("html",)) is not None
    )


def test_publish_all_generates_html_when_empty():
    settings = MagicMock()
    settings.slack_webhook_url = None
    settings.email_to = ["b@example.com"]
    settings.email_from = "a@example.com"
    settings.email_subject_prefix = "NSO"
    settings.smtp_host = "smtp.example"
    settings.smtp_port = 587
    settings.smtp_use_tls = False
    settings.smtp_user = None
    settings.smtp_password = None

    from agent.report_format import ReportOutputs

    sent_html: list[str | None] = []

    def fake_email(plain, run_id, s, report_html=None):
        sent_html.append(report_html)

    with patch("agent.publish.email_configured", return_value=True):
        with patch("agent.publish.publish_email", fake_email):
            publish_all(
                ReportOutputs(markdown=SAMPLE, plain=SAMPLE, html=""),
                "run-1",
                settings,
            )

    assert sent_html and sent_html[0]
    assert "<h1>" in sent_html[0]
