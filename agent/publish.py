"""Publish reports to stdout, files, Slack, and email."""

from __future__ import annotations

import json
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import urllib.error
import urllib.request

from agent.config import Settings, email_configured, load_settings, validate_email_settings
from agent.report_format import ReportOutputs


def save_run_artifacts(
    snapshot: dict[str, Any],
    delta: dict[str, Any],
    report_text: str,
    settings: Settings | None = None,
) -> Path:
    s = settings or load_settings()
    s.state_dir.mkdir(parents=True, exist_ok=True)
    runs = s.state_dir / "runs"
    runs.mkdir(exist_ok=True)

    run_id = snapshot.get("run_id", "unknown")
    safe_id = run_id.replace(":", "-")
    run_dir = runs / safe_id
    run_dir.mkdir(exist_ok=True)

    (run_dir / "snapshot.json").write_text(
        json.dumps(snapshot, indent=2, default=str), encoding="utf-8"
    )
    (run_dir / "delta.json").write_text(
        json.dumps(delta, indent=2, default=str), encoding="utf-8"
    )
    (run_dir / "report.md").write_text(report_text, encoding="utf-8")

    latest = s.state_dir / "latest.json"
    latest.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    meta = {
        "run_id": run_id,
        "report_path": str(run_dir / "report.md"),
    }
    (s.state_dir / "latest.meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    return run_dir


def publish_stdout(report_text: str) -> None:
    print(report_text)


def publish_slack(report_text: str, settings: Settings | None = None) -> None:
    s = settings or load_settings()
    if not s.slack_webhook_url:
        return
    body = json.dumps({"text": report_text}).encode("utf-8")
    req = urllib.request.Request(
        s.slack_webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Slack webhook failed: {exc}") from exc


def publish_email(
    report_plain: str,
    run_id: str,
    settings: Settings | None = None,
    report_html: str | None = None,
) -> None:
    s = settings or load_settings()
    if not email_configured(s):
        return
    validate_email_settings(s)

    msg = EmailMessage()
    msg["Subject"] = f"{s.email_subject_prefix} — {run_id}"
    msg["From"] = s.email_from
    msg["To"] = ", ".join(s.email_to)
    msg.set_content(report_plain)
    if report_html:
        msg.add_alternative(report_html, subtype="html")
    _send_via_smtp(s, msg)


def send_test_email(settings: Settings | None = None) -> None:
    """Connect to SMTP and send a short test message to EMAIL_TO recipients."""
    s = settings or load_settings()
    validate_email_settings(s)

    msg = EmailMessage()
    msg["Subject"] = f"{s.email_subject_prefix} — email test"
    msg["From"] = s.email_from
    msg["To"] = ", ".join(s.email_to)
    msg.set_content(
        "NSO Summary Agent email configuration test.\n\n"
        "If you received this message, SMTP settings in .env are working."
    )
    _send_via_smtp(s, msg)


def _send_via_smtp(settings: Settings, msg: EmailMessage) -> None:
    if settings.smtp_use_tls:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            if settings.smtp_user and settings.smtp_password:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            if settings.smtp_user and settings.smtp_password:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)


def publish_all(
    report: ReportOutputs | str,
    run_id: str,
    settings: Settings | None = None,
) -> list[str]:
    """Deliver to configured channels. Returns list of channels used."""
    s = settings or load_settings()
    sent: list[str] = []
    if isinstance(report, str):
        plain = report
        html_body = None
    else:
        plain = report.plain
        html_body = report.html

    if s.slack_webhook_url:
        publish_slack(plain, s)
        sent.append("slack")
    if s.email_to:
        publish_email(plain, run_id, s, report_html=html_body)
        sent.append("email")
    return sent
