"""Tests for MCP client response parsing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.mcp_client import (
    _tool_data,
    call_mcp,
    mcp_data,
    mcp_error_message,
    mcp_is_error,
    mcp_walk_root,
    unwrap_mcp_data,
)


def test_tool_data_prefers_structured_data():
    result = SimpleNamespace(
        data={"status": "success", "data": {"services": []}},
        structured_content=None,
        content=[],
    )
    assert _tool_data(result) == {"status": "success", "data": {"services": []}}


def test_tool_data_unwraps_structured_result():
    result = SimpleNamespace(
        data=None,
        structured_content={"result": {"status": "success", "data": {"in_sync": True}}},
        content=[],
    )
    parsed = _tool_data(result)
    assert parsed["data"]["in_sync"] is True


def test_mcp_data_from_success_envelope():
    assert mcp_data({"status": "success", "data": {"services": [1]}}) == {
        "services": [1]
    }


def test_mcp_data_error_returns_empty():
    assert mcp_data({"status": "error", "error_message": "boom"}) == {}


def test_mcp_is_error():
    assert mcp_is_error({"status": "error", "error_message": "x"}) is True
    assert mcp_is_error({"status": "success", "data": {}}) is False


def test_mcp_error_message():
    assert mcp_error_message({"status": "error", "error_message": "nope"}) == "nope"
    assert mcp_error_message({"status": "success", "data": {}}) is None


def test_mcp_walk_root_peels_config():
    yang = {"tailf-ned-cisco-ios-xr:bgp": {"bgp-no-instance": []}}
    result = {
        "status": "success",
        "data": {"device": "lbnl-data-sw", "config": yang},
    }
    assert mcp_walk_root(result) == yang


def test_mcp_walk_root_peels_payload():
    yang = {"tailf-ned-cisco-ios-xr:interface": {}}
    result = {
        "status": "success",
        "data": {
            "path": "tailf-ncs:devices/device=x/config",
            "depth": 1,
            "payload": yang,
        },
    }
    assert mcp_walk_root(result) == yang


def test_mcp_walk_root_keeps_legacy_data_yang():
    yang = {"tailf-ned-cisco-ios-xr:bgp": {}}
    result = {"status": "success", "data": yang}
    assert mcp_walk_root(result) == yang


def test_mcp_walk_root_preserves_truncated_marker():
    result = {
        "status": "success",
        "data": {
            "path": "x",
            "depth": 2,
            "truncated": True,
            "hint": "too big",
        },
    }
    assert mcp_walk_root(result)["truncated"] is True


def test_unwrap_mcp_data_returns_dict_only():
    assert unwrap_mcp_data({"status": "success", "data": ["a"]}) == {}
    assert unwrap_mcp_data(
        {"status": "success", "data": {"config": {"a": 1}}}
    ) == {"a": 1}


@pytest.mark.asyncio
async def test_call_mcp_retries_timeout_then_succeeds(monkeypatch, capsys):
    monkeypatch.setattr("nso_facts.mcp_client.asyncio.sleep", AsyncMock())
    ok = SimpleNamespace(
        data={"status": "success", "data": {"ok": True}},
        structured_content=None,
        content=[],
    )
    client = AsyncMock()
    client.call_tool = AsyncMock(
        side_effect=[TimeoutError("timed out"), TimeoutError("timed out"), ok]
    )
    result = await call_mcp(client, "get_services", {"service_type": "l2ptp"})
    assert result == {"status": "success", "data": {"ok": True}}
    assert client.call_tool.await_count == 3
    err = capsys.readouterr().err
    assert "retrying" in err
    assert "get_services" in err


@pytest.mark.asyncio
async def test_call_mcp_does_not_retry_value_error():
    client = AsyncMock()
    client.call_tool = AsyncMock(side_effect=ValueError("bad args"))
    with pytest.raises(ValueError, match="bad args"):
        await call_mcp(client, "get_services")
    assert client.call_tool.await_count == 1


@pytest.mark.asyncio
async def test_call_mcp_returns_status_error_without_retry():
    err_envelope = SimpleNamespace(
        data={"status": "error", "error_message": "not found"},
        structured_content=None,
        content=[],
    )
    client = AsyncMock()
    client.call_tool = AsyncMock(return_value=err_envelope)
    result = await call_mcp(client, "get_services")
    assert result == {"status": "error", "error_message": "not found"}
    assert client.call_tool.await_count == 1


@pytest.mark.asyncio
async def test_call_mcp_raises_after_retries_exhausted(monkeypatch, capsys):
    monkeypatch.setattr("nso_facts.mcp_client.asyncio.sleep", AsyncMock())
    client = AsyncMock()
    client.call_tool = AsyncMock(side_effect=ConnectionError("reset"))
    with pytest.raises(ConnectionError, match="reset"):
        await call_mcp(client, "list_devices")
    assert client.call_tool.await_count == 3
    assert "retrying" in capsys.readouterr().err
