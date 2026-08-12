"""Tests for shared dry-run / publish resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import Settings, resolve_dry_run


def _settings(*, dry_run: bool) -> Settings:
    return Settings(
        mcp_server_cmd="cmd",
        mcp_server_args=[],
        mcp_env={},
        fabric_api_key="sk-test",
        fabric_api_url="https://ai.fabric-testbed.net",
        fabric_model="gpt-oss-20b",
        state_dir=Path("/tmp/state"),
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


def test_default_env_dry_run_true():
    assert resolve_dry_run(settings=_settings(dry_run=True)) is True


def test_env_dry_run_false_allows_publish():
    assert resolve_dry_run(settings=_settings(dry_run=False)) is False


def test_publish_overrides_dry_run_env():
    assert (
        resolve_dry_run(settings=_settings(dry_run=True), publish=True) is False
    )


def test_dry_run_flag_forces_dry():
    assert (
        resolve_dry_run(settings=_settings(dry_run=False), dry_run_flag=True)
        is True
    )


def test_publish_and_dry_run_conflict():
    with pytest.raises(ValueError, match="either --publish or --dry-run"):
        resolve_dry_run(
            settings=_settings(dry_run=True),
            publish=True,
            dry_run_flag=True,
        )
