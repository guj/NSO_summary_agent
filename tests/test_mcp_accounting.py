"""Tests for per-run MCP call accounting."""

from io import StringIO

import pytest

from nso_facts.mcp_accounting import (
    classify_mcp_tool,
    current_mcp_stats,
    format_mcp_accounting,
    mcp_call_record,
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
    assert classify_mcp_tool("get_bgp_summary") == "device"
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


def test_counts_so_far_are_read_without_stopping_the_count():
    start_mcp_accounting()
    record_mcp_call("get_services", {"service_type": "l3rt"})
    record_mcp_call("exec_show", {"device_name": "mich-data-sw"}, cached=True)
    so_far = current_mcp_stats()
    record_mcp_call("exec_show", {"device_name": "kans-data-sw"})

    assert (so_far.total, so_far.cache_hits) == (2, 1)
    assert stop_mcp_accounting().total == 3


def test_record_kept_with_the_scan_counts_the_calls_made_during_llm_investigations():
    start_mcp_accounting()
    set_mcp_stage("spines")
    for n in range(3):
        record_mcp_call("exec_show", {"device_name": "mich-data-sw"}, cached=n == 0)
    for stage in ("dataplane", "drill"):
        set_mcp_stage(stage)
        record_mcp_call("exec_show", {"device_name": "kans-data-sw"})
    stats = stop_mcp_accounting()

    assert mcp_call_record(stats) == {"total": 5, "wire": 4, "cache_hits": 1, "during_llm": 2}


def _one_call():
    start_mcp_accounting()
    set_mcp_stage("spines")
    record_mcp_call("exec_show", {"device_name": "pe1", "input_command": "route 192.0.2.1"})
    return stop_mcp_accounting()


def test_the_call_list_goes_to_the_scans_folder_and_only_the_totals_are_printed(tmp_path):
    buf = StringIO()
    print_mcp_accounting(_one_call(), file=buf, calls_dir=tmp_path, mask=lambda text: text.replace("192", "xxx"))

    calls = tmp_path / "mcp_calls.txt"
    assert calls.read_text() == "[mcp] 1 spines device exec_show device=pe1 route xxx.0.2.1\n"
    assert buf.getvalue().splitlines() == [
        "[mcp] total=1 wire=1 cache_hits=0 wired_nso=0 wired_device=1 nso=0 device=1 other=0",
        "[mcp] stage spines=1",
        f"[mcp] each call: {calls}",
    ]


def test_the_call_list_is_printed_as_before_when_the_scan_saved_no_folder(tmp_path):
    for calls_dir in (None, tmp_path / "not-saved"):
        buf = StringIO()
        print_mcp_accounting(_one_call(), file=buf, calls_dir=calls_dir)

        assert "[mcp] 1 spines device exec_show device=pe1 route 192.0.2.1" in buf.getvalue()
    assert not (tmp_path / "not-saved").exists()


@pytest.mark.asyncio
async def test_a_saved_scan_keeps_its_call_list_in_its_folder_with_the_nso_address_masked(
    nso_env, llm_requests, tmp_path, monkeypatch, capsys
):
    from diagnostic_mas import run as run_mod
    from test_diagnostic_skip_llm import _args, _run_offline

    stats = _one_call()
    monkeypatch.setattr(run_mod, "stop_mcp_accounting", lambda: stats)
    # Saved but sent nowhere: no channel or Pushgateway, whatever a checkout's .env holds.
    for name in ("SLACK_WEBHOOK_URL", "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID", "EMAIL_TO", "EMAIL_FROM",
                 "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "PROMETHEUS_PUSHGATEWAY_URL"):
        monkeypatch.delenv(name, raising=False)
    args = _args(skip_llm=True)
    args.dry_run, args.publish = False, True

    assert await _run_offline(args) == 0

    [calls] = (tmp_path / "diagnostic_mas" / "runs").glob("*/mcp_calls.txt")
    assert calls.read_text() == "[mcp] 1 spines device exec_show device=pe1 route xxx.x.x.1\n"
    err = capsys.readouterr().err
    assert "[mcp] total=1 " in err and f"[mcp] each call: {calls}" in err and "[mcp] 1 spines" not in err
