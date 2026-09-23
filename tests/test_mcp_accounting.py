"""Tests for per-run MCP call accounting."""

from io import StringIO

from nso_facts.mcp_accounting import (
    classify_mcp_tool,
    format_mcp_accounting,
    print_mcp_accounting,
    record_mcp_call,
    set_mcp_stage,
    start_mcp_accounting,
    stop_mcp_accounting,
)


def test_classify_nso_and_device():
    assert classify_mcp_tool("get_services") == "nso"
    assert classify_mcp_tool("list_devices") == "nso"
    assert classify_mcp_tool("explore_nso_path") == "nso"
    assert classify_mcp_tool("exec_show") == "device"
    assert classify_mcp_tool("check_isis_adjacencies") == "device"
    assert classify_mcp_tool("get_hardware_health") == "device"
    assert classify_mcp_tool("totally_unknown_tool") == "other"


def test_record_inactive_is_noop():
    stop_mcp_accounting()  # ensure cleared
    record_mcp_call("get_services", {"service_type": "l2ptp"})
    stats = stop_mcp_accounting()
    assert stats.total == 0


def test_record_and_format():
    start_mcp_accounting()
    set_mcp_stage("spines")
    record_mcp_call("get_services", {"service_type": "l2sts", "service_name": "foo"})
    set_mcp_stage("dataplane")
    record_mcp_call(
        "exec_show",
        {"device_name": "lbnl-data-sw", "input_command": "show bgp summary"},
    )
    record_mcp_call(
        "exec_show",
        {"device_name": "lbnl-data-sw", "input_command": "show bgp summary"},
        cached=True,
    )
    set_mcp_stage(None)
    record_mcp_call("mystery_tool", {})
    stats = stop_mcp_accounting()

    assert stats.total == 4
    assert stats.wire == 3
    assert stats.cache_hits == 1
    assert stats.wired_nso == 1
    assert stats.wired_device == 1  # one wire + one cache_hit for exec_show
    assert stats.count("nso") == 1
    assert stats.count("device") == 2
    assert stats.count("other") == 1
    assert stats.stage_counts() == {"spines": 1, "dataplane": 2, "other": 1}

    lines = format_mcp_accounting(stats)
    assert lines[0] == (
        "[mcp] total=4 wire=3 cache_hits=1 "
        "wired_nso=1 wired_device=1 nso=1 device=2 other=1"
    )
    assert lines[1] == "[mcp] stage spines=1 dataplane=2 other=1"
    assert lines[2] == "[mcp] 1 spines nso get_services l2sts foo"
    assert lines[3] == (
        "[mcp] 2 dataplane device exec_show device=lbnl-data-sw show bgp summary"
    )
    assert lines[4] == (
        "[mcp] 3 dataplane device exec_show cache_hit "
        "device=lbnl-data-sw show bgp summary"
    )
    assert lines[5] == "[mcp] 4 other mystery_tool"


def test_stage_order_prefers_pipeline_names():
    start_mcp_accounting()
    set_mcp_stage("drill")
    record_mcp_call("exec_show", {"device_name": "a", "input_command": "x"})
    set_mcp_stage("focus")
    record_mcp_call("list_devices")
    set_mcp_stage("spines")
    record_mcp_call("get_services")
    stats = stop_mcp_accounting()
    lines = format_mcp_accounting(stats)
    assert lines[1] == "[mcp] stage focus=1 spines=1 drill=1"


def test_print_mcp_accounting_to_stream():
    start_mcp_accounting()
    set_mcp_stage("focus")
    record_mcp_call("list_devices")
    stats = stop_mcp_accounting()
    buf = StringIO()
    print_mcp_accounting(stats, file=buf)
    text = buf.getvalue()
    assert "[mcp] total=1 wire=1 cache_hits=0 wired_nso=1 wired_device=0 nso=1 device=0 other=0" in text
    assert "[mcp] stage focus=1" in text
    assert "[mcp] 1 focus nso list_devices" in text
