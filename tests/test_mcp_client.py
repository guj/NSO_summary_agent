"""Tests for MCP client response parsing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.mcp_client import (
    _tool_data,
    build_mcp_payload,
    call_mcp,
    mcp_data,
    mcp_error_message,
    mcp_is_error,
    mcp_walk_root,
    tool_schema_uses_params_wrapper,
    unwrap_mcp_data,
)


def _flat_tool(name: str, props: dict | None = None):
    props = props if props is not None else {"service_type": {"type": "string"}}
    return SimpleNamespace(
        name=name,
        inputSchema={
            "type": "object",
            "properties": props,
            "required": list(props.keys()),
        },
    )


def _wrapped_tool(name: str):
    return SimpleNamespace(
        name=name,
        inputSchema={
            "type": "object",
            "properties": {
                "params": {"type": "object", "additionalProperties": True}
            },
            "required": ["params"],
        },
    )


def _client_with_tools(*tools, call_side_effect=None, call_return=None):
    client = AsyncMock()
    client.list_tools = AsyncMock(return_value=list(tools))
    if call_side_effect is not None:
        client.call_tool = AsyncMock(side_effect=call_side_effect)
    else:
        client.call_tool = AsyncMock(return_value=call_return)
    return client


def test_tool_schema_uses_params_wrapper():
    assert tool_schema_uses_params_wrapper(
        {"properties": {"params": {"type": "object"}}}
    )
    assert not tool_schema_uses_params_wrapper(
        {"properties": {"device_name": {"type": "string"}}}
    )
    assert not tool_schema_uses_params_wrapper(None)


def test_build_mcp_payload_wrap_vs_flat():
    args = {"device_name": "renc", "input_command": "version"}
    assert build_mcp_payload(args, wrap_params=True) == {"params": args}
    assert build_mcp_payload(args, wrap_params=False) == args


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
    client = _client_with_tools(
        _flat_tool("get_services"),
        call_side_effect=[TimeoutError("timed out"), TimeoutError("timed out"), ok],
    )
    result = await call_mcp(client, "get_services", {"service_type": "l2ptp"})
    assert result == {"status": "success", "data": {"ok": True}}
    assert client.call_tool.await_count == 3
    assert client.call_tool.await_args.args[1] == {"service_type": "l2ptp"}
    err = capsys.readouterr().err
    assert "retrying" in err
    assert "get_services" in err


@pytest.mark.asyncio
async def test_call_mcp_uses_params_wrapper_when_schema_has_params():
    ok = SimpleNamespace(
        data={"status": "success", "data": {"ok": True}},
        structured_content=None,
        content=[],
    )
    client = _client_with_tools(_wrapped_tool("get_services"), call_return=ok)
    await call_mcp(client, "get_services", {"service_type": "l2ptp"})
    assert client.call_tool.await_args.args[1] == {
        "params": {"service_type": "l2ptp"}
    }


@pytest.mark.asyncio
async def test_call_mcp_uses_flat_args_when_schema_is_flat():
    ok = SimpleNamespace(
        data={"status": "success", "data": {"ok": True}},
        structured_content=None,
        content=[],
    )
    client = _client_with_tools(
        _flat_tool(
            "exec_show",
            {"device_name": {"type": "string"}, "input_command": {"type": "string"}},
        ),
        call_return=ok,
    )
    await call_mcp(
        client,
        "exec_show",
        {"device_name": "renc", "input_command": "version"},
    )
    assert client.call_tool.await_args.args[1] == {
        "device_name": "renc",
        "input_command": "version",
    }


@pytest.mark.asyncio
async def test_call_mcp_does_not_retry_value_error():
    client = _client_with_tools(
        _flat_tool("get_services"),
        call_side_effect=ValueError("bad args"),
    )
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
    client = _client_with_tools(_flat_tool("get_services"), call_return=err_envelope)
    result = await call_mcp(client, "get_services")
    assert result == {"status": "error", "error_message": "not found"}
    assert client.call_tool.await_count == 1


@pytest.mark.asyncio
async def test_call_mcp_raises_after_retries_exhausted(monkeypatch, capsys):
    monkeypatch.setattr("nso_facts.mcp_client.asyncio.sleep", AsyncMock())
    client = _client_with_tools(
        _flat_tool("list_devices", {}),
        call_side_effect=ConnectionError("reset"),
    )
    with pytest.raises(ConnectionError, match="reset"):
        await call_mcp(client, "list_devices")
    assert client.call_tool.await_count == 3
    assert "retrying" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_call_mcp_run_scoped_cache_hits():
    from nso_facts.mcp_accounting import start_mcp_accounting, stop_mcp_accounting
    from nso_facts.mcp_client import start_mcp_cache, stop_mcp_cache

    start_mcp_cache()
    start_mcp_accounting()
    try:
        ok = SimpleNamespace(
            data={"status": "success", "data": {"device": "renc"}},
            structured_content=None,
            content=[],
        )
        client = _client_with_tools(
            _flat_tool("get_device_config", {"device_name": {"type": "string"}}),
            call_return=ok,
        )
        params = {"device_name": "renc-data-sw"}
        first = await call_mcp(client, "get_device_config", params)
        second = await call_mcp(client, "get_device_config", params)
        assert first == second
        assert client.call_tool.await_count == 1
        assert client.list_tools.await_count == 1
        stats = stop_mcp_accounting()
        assert stats.total == 2
        assert stats.wire == 1
        assert stats.cache_hits == 1
        assert stats.records[1].cached is True
    finally:
        stop_mcp_cache()
        stop_mcp_accounting()


@pytest.mark.asyncio
async def test_call_mcp_does_not_cache_errors():
    from nso_facts.mcp_client import start_mcp_cache, stop_mcp_cache

    start_mcp_cache()
    try:
        err = SimpleNamespace(
            data={"status": "error", "error_message": "boom"},
            structured_content=None,
            content=[],
        )
        client = _client_with_tools(
            _flat_tool("get_device_config", {"device_name": {"type": "string"}}),
            call_return=err,
        )
        params = {"device_name": "x"}
        assert (await call_mcp(client, "get_device_config", params))["status"] == "error"
        assert (await call_mcp(client, "get_device_config", params))["status"] == "error"
        assert client.call_tool.await_count == 2
    finally:
        stop_mcp_cache()


@pytest.mark.asyncio
async def test_call_mcp_quarantines_device_after_timeout_error():
    from nso_facts.mcp_client import (
        quarantined_devices,
        start_mcp_cache,
        stop_mcp_cache,
    )

    start_mcp_cache()
    try:
        err = SimpleNamespace(
            data={
                "status": "error",
                "error_message": (
                    "HTTPSConnectionPool(host='192.168.11.222', port=443): "
                    "Read timed out. (read timeout=20)"
                ),
            },
            structured_content=None,
            content=[],
        )
        ok_other = SimpleNamespace(
            data={"status": "success", "data": {"ok": True}},
            structured_content=None,
            content=[],
        )

        async def _side_effect(tool, payload):
            device = (payload or {}).get("device_name")
            if device == "lbnl-data-sw":
                return err
            return ok_other

        client = AsyncMock()
        client.list_tools = AsyncMock(
            return_value=[
                _flat_tool(
                    "check_isis_adjacencies",
                    {"device_name": {"type": "string"}},
                ),
                _flat_tool(
                    "exec_show",
                    {
                        "device_name": {"type": "string"},
                        "input_command": {"type": "string"},
                    },
                ),
            ]
        )
        client.call_tool = AsyncMock(side_effect=_side_effect)

        first = await call_mcp(
            client, "check_isis_adjacencies", {"device_name": "lbnl-data-sw"}
        )
        assert first["status"] == "error"
        assert "lbnl-data-sw" in quarantined_devices()

        second = await call_mcp(
            client,
            "exec_show",
            {"device_name": "lbnl-data-sw", "input_command": "bgp summary"},
        )
        assert second["status"] == "error"
        assert "quarantined" in second["error_message"]
        # First wire only — second call skipped.
        assert client.call_tool.await_count == 1

        # Other devices still reachable.
        third = await call_mcp(
            client, "check_isis_adjacencies", {"device_name": "renc-data-sw"}
        )
        assert third["status"] == "success"
        assert client.call_tool.await_count == 2
    finally:
        stop_mcp_cache()


@pytest.mark.asyncio
async def test_call_mcp_quarantine_still_serves_cache_hits():
    from nso_facts.mcp_client import start_mcp_cache, stop_mcp_cache

    start_mcp_cache()
    try:
        ok = SimpleNamespace(
            data={"status": "success", "data": {"config": {"x": 1}}},
            structured_content=None,
            content=[],
        )
        err = SimpleNamespace(
            data={
                "status": "error",
                "error_message": "Read timed out. (read timeout=20)",
            },
            structured_content=None,
            content=[],
        )
        client = _client_with_tools(
            _flat_tool("get_device_config", {"device_name": {"type": "string"}}),
            _flat_tool("exec_show", {"device_name": {"type": "string"}}),
        )
        client.call_tool = AsyncMock(side_effect=[ok, err])

        params = {"device_name": "lbnl-data-sw"}
        assert (await call_mcp(client, "get_device_config", params))[
            "status"
        ] == "success"
        # Live timeout on another tool quarantines the device.
        assert (
            await call_mcp(client, "exec_show", params)
        )["status"] == "error"
        # Cached config still returned without a third wire call.
        cached = await call_mcp(client, "get_device_config", params)
        assert cached["status"] == "success"
        assert client.call_tool.await_count == 2
    finally:
        stop_mcp_cache()


def test_ingest_quarantined_devices_opens_issue(monkeypatch):
    from diagnostic_mas.case import CaseFile, Budget
    from diagnostic_mas.ingest import ingest_quarantined_devices

    monkeypatch.setattr(
        "nso_facts.mcp_client.quarantined_devices",
        lambda: {
            "lbnl-data-sw": (
                "HTTPSConnectionPool(host='192.168.11.246', port=443): "
                "Read timed out. (read timeout=20)"
            ),
        },
    )
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    ids = ingest_quarantined_devices(case)
    assert len(ids) == 1
    assert case.issues[0]["code"] == "device_live_unreachable"
    assert case.issues[0]["devices"] == ["lbnl-data-sw"]
    msg = case.issues[0]["message"]
    assert "automated NSO live-MCP collection timed out" in msg
    assert "read timeout=20s" in msg
    assert "live unreachable" not in msg
    assert "does not prove the device is down" in msg or "do not treat as device down" in msg or (
        "manual NSO connectivity may still succeed" in msg.lower()
    )
    # Idempotent
    assert ingest_quarantined_devices(case) == []
    assert len(case.issues) == 1


def test_live_mcp_failure_kind_timeout_vs_unreachable():
    from nso_facts.mcp_client import (
        format_live_mcp_quarantine_prose,
        is_strong_device_unreachability,
        live_mcp_failure_kind,
        parse_live_mcp_failed_operation,
        parse_live_mcp_read_timeout_sec,
    )

    timeout = (
        "exec_show: HTTPSConnectionPool(host='192.168.11.246', port=443): "
        "Read timed out. (read timeout=10)"
    )
    assert live_mcp_failure_kind(timeout) == "timeout"
    assert is_strong_device_unreachability(timeout) is False
    assert parse_live_mcp_failed_operation(timeout) == "exec_show"
    assert parse_live_mcp_read_timeout_sec(timeout) == "10"
    from nso_facts.mcp_client import parse_live_mcp_collection_phase

    isis = (
        "exec_show (isis adjacency): HTTPSConnectionPool"
        "(host='192.168.11.246', port=443): Read timed out. (read timeout=10)"
    )
    assert parse_live_mcp_failed_operation(isis) == "exec_show (isis adjacency)"
    assert parse_live_mcp_collection_phase(isis) == "IS-IS collection"
    prose = format_live_mcp_quarantine_prose("star-data-sw", timeout)
    assert "automated NSO live-MCP collection timed out on exec_show" in prose
    assert "read timeout=10s" in prose
    assert "manual nso connectivity may still succeed" in prose.lower()
    assert "live unreachable" not in prose

    strong = "device star-data-sw unreachable via NED"
    assert live_mcp_failure_kind(strong) == "unreachable"
    assert is_strong_device_unreachability(strong) is True
    assert "live unreachable" in format_live_mcp_quarantine_prose("star-data-sw", strong)


@pytest.mark.asyncio
async def test_call_mcp_without_cache_always_wires():
    ok = SimpleNamespace(
        data={"status": "success", "data": {}},
        structured_content=None,
        content=[],
    )
    client = _client_with_tools(_flat_tool("list_devices", {}), call_return=ok)
    await call_mcp(client, "list_devices")
    await call_mcp(client, "list_devices")
    assert client.call_tool.await_count == 2
    assert client.call_tool.await_count == 2


@pytest.mark.asyncio
async def test_live_verification_requires_successful_live_query():
    from nso_facts.mcp_client import (
        start_mcp_cache, stop_mcp_cache, successful_live_devices,
    )
    start_mcp_cache()
    try:
        ok = SimpleNamespace(data={"status": "success", "data": {"result": "up"}},
                             structured_content=None, content=[])
        client = _client_with_tools(
            _flat_tool("exec_show", {"device_name": {"type": "string"}}),
            call_return=ok,
        )
        await call_mcp(client, "get_device_config", {"device_name": "scm"})
        assert successful_live_devices() == []
        await call_mcp(client, "exec_show", {"device_name": "cern"})
        assert successful_live_devices() == ["cern"]
        client.call_tool.return_value = SimpleNamespace(
            data={"status": "error", "error_message": "query failed"},
            structured_content=None, content=[])
        await call_mcp(client, "exec_show", {"device_name": "scm"})
        assert successful_live_devices() == ["cern"]
    finally:
        stop_mcp_cache()
    assert successful_live_devices() == []


@pytest.mark.asyncio
async def test_archive_preserves_full_wire_response_and_cache(tmp_path):
    import json
    import stat
    from nso_facts.mcp_archive import archive_mcp_results
    from nso_facts.mcp_client import start_mcp_cache, stop_mcp_cache
    text = 'configuration\n' * 10000
    result = SimpleNamespace(data={'status': 'success', 'data': {'result': text}},
                             structured_content={'extra': 'retained'}, content=[])
    client = _client_with_tools(
        _flat_tool('exec_show', {'device_name': {'type': 'string'}}), call_return=result)
    start_mcp_cache()
    try:
        with archive_mcp_results(tmp_path, 'run-test') as path:
            first = await call_mcp(client, 'exec_show', {'device_name': 'cern'})
            second = await call_mcp(client, 'exec_show', {'device_name': 'cern'})
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            assert rows[0]['response']['data']['result'] == text
            assert rows[0]['raw_response']['structured_content'] == {'extra': 'retained'}
            assert rows[1]['source'] == 'cache'
            assert rows[1]['response'] == first == second
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert client.call_tool.await_count == 1
    finally:
        stop_mcp_cache()


@pytest.mark.asyncio
async def test_archive_records_failure_without_changing_retries(tmp_path):
    import json
    from nso_facts.mcp_archive import archive_mcp_results
    from nso_facts.mcp_client import start_mcp_cache, stop_mcp_cache
    result = SimpleNamespace(data={'status': 'error', 'error_message': 'bad command'},
                             structured_content=None, content=[])
    client = _client_with_tools(_flat_tool('exec_show', {}), call_return=result)
    start_mcp_cache()
    try:
        with archive_mcp_results(tmp_path, 'errors') as path:
            await call_mcp(client, 'exec_show', {'device_name': 'cern'})
            client.call_tool.side_effect = [ConnectionError('transport failed'), result]
            await call_mcp(client, 'exec_show', {'device_name': 'cern'})
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert [r['source'] for r in rows] == ['wire', 'transport_error', 'wire']
        assert rows[0]['response']['status'] == 'error'
        assert rows[-1]['attempt'] == 2
    finally:
        stop_mcp_cache()
