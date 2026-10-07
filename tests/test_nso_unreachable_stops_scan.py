"""Tests that a scan stops when NSO itself stops answering, instead of blaming devices."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from diagnostic_mas import run as run_mod
from nso_facts.mcp_client import (
    call_mcp,
    nso_watch,
    quarantined_devices,
    start_mcp_cache,
    stop_mcp_cache,
)
from test_diagnostic_skip_llm import _Session, _args, llm_requests, nso_env  # noqa: F401

TIMEOUT = "Request timed out after 30s: https://192.0.2.1:443/restconf/data/tailf-ncs:devices"
EXEC_SHOW = {"device_name": "pe1", "input_command": "version"}
# What the MCP server relays when it cannot open a connection to NSO at all.
_POOL = "HTTPSConnectionPool(host='192.0.2.1', port=443): Max retries exceeded with url: /restconf"
NO_CONNECTION = [
    f"{_POOL} (Caused by NewConnectionError('Failed to establish a new connection: "
    "[Errno 65] No route to host'))",
    f"{_POOL} (Caused by NameResolutionError(\"Failed to resolve 'nso.example' "
    "([Errno 8] nodename nor servname provided, or not known)\"))",
    "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))",
]


def _result(data: dict) -> SimpleNamespace:
    return SimpleNamespace(data=data, structured_content=None, content=[])


class _FakeNso:
    """MCP client where live device queries time out; NSO's own answer is set per test."""

    def __init__(self, *, nso_answers: bool, probe_raises: Exception | None = None,
                 failure: str = TIMEOUT):
        self.nso_answers = nso_answers
        self.probe_raises = probe_raises
        self.failure = failure
        self.calls: list[str] = []

    async def list_tools(self):
        return []

    async def call_tool(self, tool, payload):
        self.calls.append(tool)
        await asyncio.sleep(0)  # let other in-flight calls run, as a wire call would
        if tool == "list_devices":
            if self.probe_raises is not None:
                raise self.probe_raises
            if self.nso_answers:
                return _result({"status": "success", "data": {"device": []}})
            return _result({"status": "error", "error_message": self.failure})
        if tool == "get_device_config":
            return _result({"status": "error", "error_message": "device not found"})
        return _result({"status": "error", "error_message": self.failure})


@pytest.fixture
def mcp_run():
    """A run-scoped MCP session with the NSO watch a scan installs."""
    start_mcp_cache()
    try:
        with nso_watch() as watch:
            yield watch
    finally:
        stop_mcp_cache()


@pytest.mark.asyncio
async def test_device_timeout_is_not_blamed_on_the_device_when_nso_does_not_answer(mcp_run):
    client = _FakeNso(nso_answers=False)

    result = await call_mcp(client, "exec_show", EXEC_SHOW)

    assert result["status"] == "error"
    assert mcp_run.stopped.is_set()
    assert "Request timed out after 30s" in mcp_run.reason
    assert quarantined_devices() == {}


@pytest.mark.asyncio
async def test_device_timeout_drops_only_that_device_when_nso_answers(mcp_run):
    client = _FakeNso(nso_answers=True)

    await call_mcp(client, "exec_show", EXEC_SHOW)

    assert "pe1" in quarantined_devices()
    assert mcp_run.reason is None
    assert not mcp_run.stopped.is_set()


@pytest.mark.asyncio
async def test_no_request_is_sent_once_nso_is_known_unreachable(mcp_run):
    client = _FakeNso(nso_answers=False)
    await call_mcp(client, "exec_show", EXEC_SHOW)
    sent = len(client.calls)

    later = await call_mcp(client, "get_services", {"service_type": "l2ptp"})

    assert len(client.calls) == sent
    assert later["status"] == "error"
    assert "NSO is unreachable" in later["error_message"]


@pytest.mark.asyncio
async def test_nso_is_asked_once_and_not_retried(mcp_run):
    client = _FakeNso(nso_answers=False, probe_raises=ConnectionError("connection reset"))

    await call_mcp(client, "exec_show", EXEC_SHOW)

    assert client.calls.count("list_devices") == 1
    assert "connection reset" in mcp_run.reason


@pytest.mark.asyncio
async def test_devices_timing_out_together_share_one_nso_check(mcp_run):
    client = _FakeNso(nso_answers=False)

    await asyncio.gather(*(
        call_mcp(client, "exec_show", {"device_name": name, "input_command": "version"})
        for name in ("pe1", "pe2", "pe3", "pe4")
    ))

    assert client.calls.count("list_devices") == 1
    assert quarantined_devices() == {}


@pytest.mark.asyncio
async def test_timeout_of_a_call_that_names_no_device_also_checks_nso(mcp_run):
    client = _FakeNso(nso_answers=False)

    await call_mcp(client, "get_services", {"service_type": "l2ptp"})

    assert mcp_run.stopped.is_set()


@pytest.mark.asyncio
async def test_a_failed_device_list_is_itself_the_nso_check(mcp_run):
    client = _FakeNso(nso_answers=False)

    await call_mcp(client, "list_devices")

    assert client.calls == ["list_devices"]
    assert mcp_run.stopped.is_set()


@pytest.mark.parametrize("failure", NO_CONNECTION)
@pytest.mark.asyncio
async def test_failing_to_connect_to_nso_is_checked_like_a_timeout(mcp_run, failure):
    client = _FakeNso(nso_answers=False, failure=failure)

    await call_mcp(client, "exec_show", EXEC_SHOW)

    assert mcp_run.stopped.is_set()
    assert mcp_run.reason == failure


@pytest.mark.asyncio
async def test_a_connection_error_does_not_drop_the_device_when_nso_answers(mcp_run):
    client = _FakeNso(nso_answers=True, failure=NO_CONNECTION[0])

    await call_mcp(client, "exec_show", EXEC_SHOW)

    assert client.calls == ["exec_show", "list_devices"]
    assert mcp_run.reason is None
    assert quarantined_devices() == {}


@pytest.mark.asyncio
async def test_a_dig_reservation_limit_is_not_mistaken_for_nso_not_answering(mcp_run):
    from diagnostic_mas.dataplane_scheduler import ReservedClient

    class _DigClient(ReservedClient):
        def reserve_call(self, name, arguments):
            if name == "list_devices":
                raise RuntimeError("Concurrency reservation: scope is in use by another dig")

    client = _DigClient(_FakeNso(nso_answers=True), None, 0, {})

    await call_mcp(client, "exec_show", EXEC_SHOW)

    assert mcp_run.reason is None
    assert "pe1" in quarantined_devices()


@pytest.mark.asyncio
async def test_an_error_that_is_not_a_timeout_does_not_check_nso(mcp_run):
    client = _FakeNso(nso_answers=False)

    await call_mcp(client, "get_device_config", {"device_name": "pe1"})

    assert client.calls == ["get_device_config"]
    assert mcp_run.reason is None


# --- the scan itself -------------------------------------------------------


async def _lose_nso(*_args, **_kwargs) -> None:
    """Stand-in for collection: a device query times out and NSO does not answer."""
    await call_mcp(_FakeNso(nso_answers=False), "exec_show", EXEC_SHOW)


async def _lose_nso_then_keep_working(*_args, **_kwargs) -> None:
    await _lose_nso()
    await asyncio.sleep(3600)  # the LLM phases that would otherwise follow


async def _scan(args, *, spines) -> tuple[int, MagicMock]:
    render = MagicMock(return_value="# report\n")
    stubs = {
        "mcp_session": MagicMock(return_value=_Session()),
        "call_mcp": AsyncMock(return_value={"status": "success", "data": {"device": []}}),
        "parse_device_names": MagicMock(return_value=["pe1"]),
        "run_mandatory_spines": spines,
        "render_report": render,
    }
    with ExitStack() as stack:
        for name, stub in stubs.items():
            stack.enter_context(patch.object(run_mod, name, new=stub))
        return await asyncio.wait_for(run_mod._run(args), timeout=5), render


def _publishing(monkeypatch):
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
    return args


@pytest.mark.asyncio
async def test_scan_saves_and_publishes_nothing_when_nso_becomes_unreachable(
    nso_env, llm_requests, monkeypatch, tmp_path, capsys
):
    args = _publishing(monkeypatch)

    with patch("agent.publish.publish_slack_file") as slack:
        with patch("agent.publish.publish_email") as email:
            code, render = await _scan(args, spines=_lose_nso)

    err = capsys.readouterr().err
    assert code == run_mod.NSO_UNREACHABLE_EXIT == 3
    assert "NSO is unreachable; scan stopped" in err
    assert "Request timed out after 30s" in err
    assert not slack.called and not email.called
    assert not render.called
    assert not list(tmp_path.rglob("report.html"))
    assert not list(tmp_path.rglob("latest.json"))


@pytest.mark.asyncio
async def test_scan_stops_at_once_instead_of_running_the_remaining_phases(
    nso_env, llm_requests, capsys
):
    code, render = await _scan(_args(skip_llm=True), spines=_lose_nso_then_keep_working)

    assert code == run_mod.NSO_UNREACHABLE_EXIT
    assert "NSO is unreachable; scan stopped" in capsys.readouterr().err
    assert not render.called


@pytest.mark.asyncio
async def test_scan_with_nso_answering_finishes_normally(nso_env, llm_requests, capsys):
    async def _one_device_times_out(*_args, **_kwargs) -> None:
        await call_mcp(_FakeNso(nso_answers=True), "exec_show", EXEC_SHOW)

    code, render = await _scan(_args(skip_llm=True), spines=_one_device_times_out)

    assert code == 0
    assert render.called
    assert "NSO is unreachable" not in capsys.readouterr().err
