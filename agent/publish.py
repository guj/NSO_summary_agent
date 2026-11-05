"""Publish reports to stdout, files, Slack, and email."""

from __future__ import annotations

import json
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import urllib.error
import urllib.request

from agent.config import Settings, email_configured, load_settings, validate_email_settings
from agent.report_format import ReportOutputs


def mask_nso_address(text: str, address: str | None) -> str:
    """Hide the NSO management address in text that is printed or sent out.

    Every character but the last two becomes ``x`` (dots and colons kept), so
    ``192.0.2.146`` reads ``xxx.x.x.x46``. A letter, not ``*``, because Markdown and
    Slack read asterisks as emphasis. Only the whole address is replaced, never
    part of a longer address or name.
    """
    if not text or not address or len(address) <= 2:
        return text
    masked = re.sub(r"[^.:]", "x", address[:-2]) + address[-2:]
    pattern = r"(?<![\w.-])" + re.escape(address) + r"(?![\w-]|\.\w)"
    return re.sub(pattern, masked, text, flags=re.IGNORECASE)


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
    subject: str | None = None,
) -> None:
    s = settings or load_settings()
    if not email_configured(s):
        return
    validate_email_settings(s)

    msg = EmailMessage()
    msg["Subject"] = subject or f"{s.email_subject_prefix} — {run_id}"
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
    failures: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Deliver to configured channels. Returns list of channels used.

    Each channel is attempted on its own. With ``failures`` given, a channel that fails is
    recorded there as ``(channel, reason)`` and the others still run; without it, the
    first failure is raised.
    """
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

    def deliver(channel: str, send) -> None:
        try:
            send()
        except Exception as exc:  # noqa: BLE001
            if failures is None:
                raise
            failures.append((channel, str(exc)))
        else:
            sent.append(channel)

    if attachment_path is not None:
        if s.slack_bot_token and s.slack_channel_id:
            deliver("slack", lambda: publish_slack_file(attachment_path, md, s))
        elif s.slack_webhook_url:
            location = (f"Full HTML report: {report_url}" if report_url else
                        "Full HTML report is saved with the run artifacts; this Slack webhook cannot attach files.")
            deliver("slack", lambda: publish_slack(md + "\n\n" + location, s))
    elif s.slack_webhook_url:
        deliver("slack", lambda: publish_slack(md or plain, s))
    if s.email_to:
        if attachment_path is not None:
            deliver("email", lambda: publish_email(
                plain + "\n\nFull HTML report attached; download and open in a browser.",
                run_id, s, attachment_path=attachment_path))
        else:
            deliver("email", lambda: publish_email(plain, run_id, s, report_html=html_body))
    return sent


def publish_notice(
    text: str,
    run_id: str,
    settings: Settings | None = None,
    *,
    failures: list[tuple[str, str]] | None = None,
) -> list[str]:
    """A short notice, such as a scan that stopped without a report, through the report's channels.

    Slack by its webhook when one is set; otherwise the bot, which can only upload files, posts it
    as the comment on a small text file. Email gets its own subject. Returns the channels used;
    with ``failures`` given, a channel that fails is recorded there and the others still run.
    """
    s = settings or load_settings()
    sent: list[str] = []

    def deliver(channel: str, send) -> None:
        try:
            send()
        except Exception as exc:  # noqa: BLE001
            if failures is None:
                raise
            failures.append((channel, str(exc)))
        else:
            sent.append(channel)

    def upload() -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / f"scan-{run_id}-failed.txt"
            path.write_text(text + "\n", encoding="utf-8")
            publish_slack_file(path, text, s, title="NSO diagnostic scan failed")

    if s.slack_webhook_url:
        deliver("slack", lambda: publish_slack(text, s))
    elif s.slack_bot_token and s.slack_channel_id:
        deliver("slack", upload)
    if s.email_to:
        deliver("email", lambda: publish_email(text, run_id, s,
                                               subject=f"{s.email_subject_prefix} — {run_id} failed"))
    return sent


def publish_slack_file(path: Path, summary: str, settings: Settings, *,
                       title: str = "NSO diagnostic report") -> None:
    """Upload a file through Slack's external-upload flow (files:write), ``summary`` as its comment."""
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
        "files": json.dumps([{"id": upload["file_id"], "title": title}]),
        "channel_id": settings.slack_channel_id,
        "initial_comment": summary,
    })


def _slack_api(token: str, method: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Call one Slack Web API method; a transport failure is returned as an error result."""
    import urllib.parse

    request = urllib.request.Request(
        "https://slack.com/api/" + method,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "error": f"request failed ({exc})"}
    return result if isinstance(result, dict) else {"ok": False, "error": "unexpected response"}


def check_slack_bot(settings: Settings) -> tuple[bool, str]:
    """Verify the bot token and, when its scopes allow, that the bot is in the channel.

    Sends nothing to the channel. Reading membership needs ``channels:read``
    (``groups:read`` for a private channel); without it only the token is verified.
    """
    token = str(settings.slack_bot_token or "")
    auth = _slack_api(token, "auth.test", {})
    if not auth.get("ok"):
        return False, str(auth.get("error") or "unknown error")
    info = _slack_api(token, "conversations.info", {"channel": settings.slack_channel_id})
    if info.get("ok"):
        channel = info.get("channel") if isinstance(info.get("channel"), dict) else {}
        if channel.get("is_member"):
            return True, "token valid; bot is in the channel"
        return False, "bot is not in the channel; invite it with /invite @your-app-name"
    error = str(info.get("error") or "unknown error")
    if error == "missing_scope":
        return True, "token valid; channel membership not checked, the bot lacks the channels:read scope"
    if error == "channel_not_found":
        error += " (wrong SLACK_CHANNEL_ID, or a private channel the bot is not in)"
    return False, error


def check_email(settings: Settings) -> tuple[bool, str]:
    """Verify email settings and the SMTP connection, TLS and login. Sends no message."""
    try:
        validate_email_settings(settings)
    except ValueError as exc:
        problems = [line[2:] for line in str(exc).splitlines() if line.startswith("- ")]
        return False, "; ".join(problems) or str(exc)
    login = bool(settings.smtp_user and settings.smtp_password)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            if settings.smtp_use_tls:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if login:
                smtp.login(settings.smtp_user, settings.smtp_password)
            else:
                smtp.noop()
    except (smtplib.SMTPException, OSError) as exc:
        return False, str(exc)
    return True, f"{settings.smtp_host}:{settings.smtp_port}; " + (
        "login accepted" if login else "server reachable, no login configured"
    )


def delivery_checks(settings: Settings) -> list[tuple[str, bool, str]]:
    """One ``(channel, ok, detail)`` per configured delivery method; nothing is sent."""
    results: list[tuple[str, bool, str]] = []
    if settings.slack_bot_token and settings.slack_channel_id:
        results.append(("Slack bot", *check_slack_bot(settings)))
    elif settings.slack_bot_token or settings.slack_channel_id:
        results.append(("Slack bot", False, "set both SLACK_BOT_TOKEN and SLACK_CHANNEL_ID"))
    if settings.slack_webhook_url:
        results.append(
            ("Slack webhook", True, "configured (cannot be verified without posting a message)")
        )
    if email_configured(settings):
        results.append(("Email", *check_email(settings)))
    return results
