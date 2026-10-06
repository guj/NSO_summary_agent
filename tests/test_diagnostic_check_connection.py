"""Tests for nso-diagnostic-run --check-connection."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from diagnostic_mas import run as run_mod

TWO_DEVICES = {"status": "success", "data": {"devices": [{"name": "pe1"}, {"name": "pe2"}]}}


@pytest.fixture(autouse=True)
def nso_env(monkeypatch, tmp_path):
    """NSO settings present; LLM key blank, as shipped in .env.example."""
    monkeypatch.setenv("NSO_ADDRESS", "192.0.2.1")
    monkeypatch.setenv("NSO_PASSWORD", "test")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FABRIC_AI_API_URL", "https://llm.invalid")
    monkeypatch.setenv("FABRIC_AI_API_KEY", "")
    for name in (
        "SLACK_WEBHOOK_URL", "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID", "EMAIL_TO", "EMAIL_FROM",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_USE_TLS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def llm_requests(monkeypatch):
    """Record HTTP requests to the LLM endpoint and keep them off the network."""
    urls: list[str] = []

    def _urlopen(req, *args, **kwargs):
        urls.append(req.full_url)
        raise urllib.error.URLError("blocked in tests")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return urls


class _Response:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_llm(monkeypatch, *, key_info=404, chat={"model": "m1"}) -> list[str]:
    """Answer LLM-endpoint requests locally; an int answer is an HTTP error status."""
    urls: list[str] = []

    def _urlopen(req, *args, **kwargs):
        urls.append(req.full_url)
        answer = key_info if "/key/info" in req.full_url else chat
        if isinstance(answer, int):
            raise urllib.error.HTTPError(req.full_url, answer, "error", {}, io.BytesIO(b""))
        return _Response(answer)

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return urls


class _Client:
    async def list_tools(self):
        return [MagicMock(), MagicMock(), MagicMock()]


class _Session:
    def __init__(self, error: Exception | None = None):
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return _Client()

    async def __aexit__(self, *exc):
        return False


def _check(*, session: _Session | None = None, list_devices=TWO_DEVICES, args=()):
    """Run the real CLI with the MCP session and NSO call stubbed."""
    answer = (
        AsyncMock(side_effect=list_devices)
        if isinstance(list_devices, Exception)
        else AsyncMock(return_value=list_devices)
    )
    scan = AsyncMock()
    with patch.object(run_mod, "mcp_session", return_value=session or _Session()):
        with patch.object(run_mod, "call_mcp", new=answer):
            with patch.object(run_mod, "run_mandatory_spines", new=scan):
                code = run_mod.main(["--check-connection", *args])
    return code, answer, scan


def test_check_connection_reports_mcp_tools_and_nso_devices(capsys):
    code, _, _ = _check()

    out = capsys.readouterr().out
    assert code == 0
    assert "MCP server: OK (3 tools)" in out
    assert "NSO: OK (2 devices)" in out


def test_check_connection_does_not_scan_or_contact_the_llm(llm_requests):
    code, nso_call, scan = _check()

    assert code == 0
    assert [call.args[1] for call in nso_call.await_args_list] == ["list_devices"]
    scan.assert_not_awaited()
    assert llm_requests == []


def test_check_connection_fails_when_mcp_server_does_not_start(capsys):
    code, nso_call, _ = _check(session=_Session(error=OSError("No such file or directory")))

    assert code == 1
    assert "MCP server: FAILED — No such file or directory" in capsys.readouterr().err
    nso_call.assert_not_awaited()


def test_check_connection_fails_when_nso_returns_an_error(capsys):
    code, _, _ = _check(list_devices={"status": "error", "error_message": "401 Unauthorized"})

    assert code == 1
    assert "NSO: FAILED — 401 Unauthorized" in capsys.readouterr().err


def test_check_connection_fails_when_nso_query_raises(capsys):
    code, _, _ = _check(list_devices=TimeoutError("timed out"))

    assert code == 1
    assert "NSO: FAILED — timed out" in capsys.readouterr().err


def test_check_connection_fails_when_nso_returns_no_devices(capsys):
    code, _, _ = _check(list_devices={"status": "success", "data": {"devices": []}})

    assert code == 1
    assert "NSO: FAILED — no devices returned" in capsys.readouterr().err


def test_check_connection_reports_llm_not_configured_when_key_is_blank(capsys, llm_requests):
    code, _, _ = _check()

    assert code == 0
    assert "LLM: not configured" in capsys.readouterr().out
    assert llm_requests == []


def test_check_connection_reports_the_working_llm_model(capsys, monkeypatch):
    monkeypatch.setenv("FABRIC_AI_API_KEY", "sk-test")
    _fake_llm(monkeypatch, chat={"model": "m1"})

    code, _, _ = _check()

    assert code == 0
    assert "LLM: OK (model m1)" in capsys.readouterr().out


def test_check_connection_fails_when_the_llm_rejects_the_request(capsys, monkeypatch):
    monkeypatch.setenv("FABRIC_AI_API_KEY", "sk-bad")
    _fake_llm(monkeypatch, chat=401)

    code, _, _ = _check()

    assert code == 1
    assert "LLM: FAILED — HTTP 401" in capsys.readouterr().err


def test_check_connection_fails_without_a_model_request_when_key_is_invalid(capsys, monkeypatch):
    monkeypatch.setenv("FABRIC_AI_API_KEY", "sk-bad")
    urls = _fake_llm(monkeypatch, key_info=401)

    code, _, _ = _check()

    assert code == 1
    assert "LLM: FAILED — key invalid or revoked" in capsys.readouterr().err
    assert not any("chat/completions" in url for url in urls)


def test_check_connection_with_skip_llm_leaves_the_llm_alone(capsys, monkeypatch):
    monkeypatch.setenv("FABRIC_AI_API_KEY", "sk-test")
    urls = _fake_llm(monkeypatch)

    code, _, _ = _check(args=["--skip-llm"])

    assert code == 0
    assert urls == []
    assert "LLM:" not in capsys.readouterr().out


def test_check_connection_still_checks_the_llm_when_mcp_server_fails(capsys, monkeypatch):
    monkeypatch.setenv("FABRIC_AI_API_KEY", "sk-test")
    _fake_llm(monkeypatch)

    code, _, _ = _check(session=_Session(error=OSError("No such file or directory")))

    captured = capsys.readouterr()
    assert code == 1
    assert "MCP server: FAILED" in captured.err
    assert "LLM: OK (model m1)" in captured.out


def _fake_slack(monkeypatch, responses: dict[str, dict]) -> list[str]:
    """Answer Slack Web API calls locally; return the list of methods called."""
    called: list[str] = []

    def _urlopen(req, *args, **kwargs):
        assert req.full_url.startswith("https://slack.com/api/"), req.full_url
        method = req.full_url.rsplit("/", 1)[1]
        called.append(method)
        return _Response(responses[method])

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return called


def _slack_bot_env(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")


class _FakeSMTP:
    """Stands in for smtplib.SMTP; records what the check does."""

    calls: list[str] = []
    login_error: Exception | None = None

    def __init__(self, host, port, timeout=None):
        type(self).calls.append(f"connect {host}:{port}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def noop(self):
        type(self).calls.append("noop")

    def starttls(self, context=None):
        type(self).calls.append("starttls")

    def login(self, user, password):
        if type(self).login_error:
            raise type(self).login_error
        type(self).calls.append(f"login {user}")

    def send_message(self, msg):
        type(self).calls.append("send_message")


@pytest.fixture
def smtp(monkeypatch):
    import smtplib

    _FakeSMTP.calls = []
    _FakeSMTP.login_error = None
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setenv("EMAIL_TO", "ops@example.com")
    monkeypatch.setenv("EMAIL_FROM", "nso@example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "nso@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    return _FakeSMTP


def test_check_connection_reports_when_no_delivery_is_configured(capsys, llm_requests):
    code, _, _ = _check()

    assert code == 0
    assert "Delivery: none configured" in capsys.readouterr().out


def test_check_connection_confirms_slack_bot_is_in_the_channel(capsys, monkeypatch):
    _slack_bot_env(monkeypatch)
    _fake_slack(monkeypatch, {
        "auth.test": {"ok": True},
        "conversations.info": {"ok": True, "channel": {"is_member": True}},
    })

    code, _, _ = _check()

    assert code == 0
    assert "Slack bot: OK (token valid; bot is in the channel)" in capsys.readouterr().out


def test_check_connection_fails_when_slack_bot_is_not_in_the_channel(capsys, monkeypatch):
    _slack_bot_env(monkeypatch)
    _fake_slack(monkeypatch, {
        "auth.test": {"ok": True},
        "conversations.info": {"ok": True, "channel": {"is_member": False}},
    })

    code, _, _ = _check()

    assert code == 1
    assert "Slack bot: FAILED — bot is not in the channel" in capsys.readouterr().err


def test_check_connection_accepts_a_token_that_cannot_read_channel_membership(capsys, monkeypatch):
    _slack_bot_env(monkeypatch)
    _fake_slack(monkeypatch, {
        "auth.test": {"ok": True},
        "conversations.info": {"ok": False, "error": "missing_scope"},
    })

    code, _, _ = _check()

    assert code == 0
    assert "Slack bot: OK (token valid; channel membership not checked" in capsys.readouterr().out


def test_check_connection_fails_on_a_rejected_slack_token(capsys, monkeypatch):
    _slack_bot_env(monkeypatch)
    called = _fake_slack(monkeypatch, {"auth.test": {"ok": False, "error": "invalid_auth"}})

    code, _, _ = _check()

    assert code == 1
    assert "Slack bot: FAILED — invalid_auth" in capsys.readouterr().err
    assert called == ["auth.test"]


def test_check_connection_fails_when_only_half_the_slack_bot_settings_are_set(capsys, monkeypatch, llm_requests):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    code, _, _ = _check()

    assert code == 1
    assert "Slack bot: FAILED — set both SLACK_BOT_TOKEN and SLACK_CHANNEL_ID" in capsys.readouterr().err
    assert llm_requests == []


def test_check_connection_notes_a_webhook_without_posting_to_it(capsys, monkeypatch, llm_requests):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/x")

    code, _, _ = _check()

    assert code == 0
    assert "Slack webhook: configured (cannot be verified without posting a message)" in capsys.readouterr().out
    assert llm_requests == []


def test_check_connection_verifies_smtp_login_without_sending_mail(capsys, smtp, llm_requests):
    code, _, _ = _check()

    assert code == 0
    assert "Email: OK (smtp.example.com:587; login accepted)" in capsys.readouterr().out
    assert "login nso@example.com" in smtp.calls
    assert "send_message" not in smtp.calls


def test_check_connection_fails_when_smtp_rejects_the_login(capsys, smtp, llm_requests):
    import smtplib

    smtp.login_error = smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

    code, _, _ = _check()

    assert code == 1
    assert "Email: FAILED — " in capsys.readouterr().err


def test_check_connection_fails_on_incomplete_email_settings(capsys, monkeypatch, llm_requests):
    monkeypatch.setenv("EMAIL_TO", "ops@example.com")

    code, _, _ = _check()

    assert code == 1
    assert "Email: FAILED — SMTP_HOST is required when EMAIL_TO is set" in capsys.readouterr().err
