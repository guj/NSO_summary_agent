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
    """Post to Slack Incoming Webhook.

    ``report_text`` may be markdown or already-converted mrkdwn; markdown is
    converted to Slack blocks so headings/bold/lists render.
    """
    s = settings or load_settings()
    if not s.slack_webhook_url:
        return
    from agent.markdown_channels import slack_payload_from_markdown

    payload = slack_payload_from_markdown(report_text)
    body = json.dumps(payload).encode("utf-8")
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
    attachment_path: Path | None = None,
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
    html_body = (report_html or "").strip()
    if not html_body:
        from agent.markdown_channels import markdown_to_html

        # Plain may already be stripped; prefer regenerating from itself as MD-ish
        html_body = markdown_to_html(report_plain)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    if attachment_path is not None:
        msg.add_attachment(attachment_path.read_bytes(), maintype="text", subtype="html",
                           filename=attachment_path.name)
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
    *,
    attachment_path: Path | None = None,
    report_url: str | None = None,
) -> list[str]:
    """Deliver to configured channels. Returns list of channels used."""
    from agent.markdown_channels import (
        markdown_to_html,
        markdown_to_plain,
        outputs_from_markdown,
    )

    s = settings or load_settings()
    sent: list[str] = []
    if isinstance(report, str):
        outs = outputs_from_markdown(report)
        md = outs.markdown
        plain = outs.plain
        html_body = outs.html
    else:
        md = report.markdown or report.plain or ""
        plain = report.plain or markdown_to_plain(md)
        html_body = (report.html or "").strip() or markdown_to_html(md)

    if attachment_path is not None:
        if s.slack_bot_token and s.slack_channel_id:
            publish_slack_file(attachment_path, md, s)
            sent.append("slack")
        elif s.slack_webhook_url:
            location = (f"Full HTML report: {report_url}" if report_url else
                        "Full HTML report is saved with the run artifacts; this Slack webhook cannot attach files.")
            publish_slack(md + "\n\n" + location, s)
            sent.append("slack")
    elif s.slack_webhook_url:
        publish_slack(md or plain, s)
        sent.append("slack")
    if s.email_to:
        if attachment_path is not None:
            publish_email(plain + "\n\nFull HTML report attached; download and open in a browser.",
                          run_id, s, attachment_path=attachment_path)
        else:
            publish_email(plain, run_id, s, report_html=html_body)
        sent.append("email")
    return sent


def publish_slack_file(path: Path, summary: str, settings: Settings) -> None:
    """Upload HTML through Slack's external-upload flow (files:write)."""
    import urllib.parse

    def api(method: str, fields: dict) -> dict:
        request = urllib.request.Request(
            "https://slack.com/api/" + method,
            data=urllib.parse.urlencode(fields).encode(),
            headers={"Authorization": f"Bearer {settings.slack_bot_token}",
                     "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read())
        if not result.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {result.get('error', 'unknown error')}")
        return result

    content = path.read_bytes()
    upload = api("files.getUploadURLExternal", {"filename": path.name, "length": len(content)})
    url = upload["upload_url"]
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".slack.com"):
        raise RuntimeError("Slack returned an unexpected upload URL")
    request = urllib.request.Request(url, data=content, method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(request, timeout=60) as response:
        response.read()
    api("files.completeUploadExternal", {
        "files": json.dumps([{"id": upload["file_id"], "title": "NSO diagnostic report"}]),
        "channel_id": settings.slack_channel_id,
        "initial_comment": summary,
    })
