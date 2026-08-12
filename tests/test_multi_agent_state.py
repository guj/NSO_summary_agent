"""Tests for multi-agent state path and persist helpers."""

from __future__ import annotations

from pathlib import Path

from agent.config import Settings
from multi_agent.state_paths import (
    load_previous_latest,
    multi_agent_run_dir,
    multi_agent_state_dir,
    persist_multi_agent_run,
    save_multi_agent_latest,
)


def _settings(*, state_dir: Path, dry_run: bool = True) -> Settings:
    return Settings(
        mcp_server_cmd="cmd",
        mcp_server_args=[],
        mcp_env={},
        fabric_api_key="sk-test",
        fabric_api_url="https://ai.fabric-testbed.net",
        fabric_model="gpt-oss-20b",
        state_dir=state_dir,
        dry_run=dry_run,
        ignore_service_types=frozenset(),
        max_service_types=10,
        report_sections=("executive", "devices"),
        slack_webhook_url=None,
        smtp_host=None,
        smtp_port=587,
        smtp_user=None,
        smtp_password=None,
        smtp_use_tls=True,
        email_from=None,
        email_to=[],
        email_subject_prefix="NSO Summary",
        prometheus_pushgateway_url=None,
        prometheus_job="nso-summary",
        prometheus_instance="default",
        prompts_dir=Path("/tmp/prompts"),
        topology_force_update=False,
        interface_equivalences_file=None,
        iface_troubleshoot_max_tool_rounds=10,
        iface_troubleshoot_disable=False,
    )


def test_multi_agent_state_dir_is_subdir(tmp_path: Path):
    s = _settings(state_dir=tmp_path / "state")
    assert multi_agent_state_dir(s) == tmp_path / "state" / "multi_agent"


def test_load_previous_missing_returns_none(tmp_path: Path):
    assert load_previous_latest(tmp_path) is None


def test_save_and_load_latest_roundtrip(tmp_path: Path):
    payload = {
        "run_id": "r1",
        "pipeline": "multi-agent",
        "counts": {},
        "services": {},
    }
    save_multi_agent_latest(tmp_path, payload)
    assert load_previous_latest(tmp_path) == payload


def test_run_dir_sanitizes_run_id(tmp_path: Path):
    d = multi_agent_run_dir(tmp_path, "2026-08-10T22:21:47Z")
    assert d == tmp_path / "runs" / "2026-08-10T22-21-47Z"


def test_persist_writes_latest_and_run(tmp_path: Path):
    run_dir = persist_multi_agent_run(
        tmp_path,
        run_id="2026-08-10T01:02:03Z",
        counts={
            "l2ptp": {
                "total": 1,
                "up": 1,
                "down": 0,
                "degraded": 0,
                "unknown": 0,
            }
        },
        services={},
        delta={"first_run": True},
        report="# hi\n",
        artifact_files={"merged.json": "{}"},
    )
    assert (run_dir / "report.md").read_text(encoding="utf-8") == "# hi\n"
    assert (run_dir / "merged.json").read_text(encoding="utf-8") == "{}"
    assert (run_dir / "delta.json").is_file()
    latest = load_previous_latest(tmp_path)
    assert latest is not None
    assert latest["pipeline"] == "multi-agent"
    assert latest["counts"]["l2ptp"]["total"] == 1
