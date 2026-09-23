"""Tests for diagnostic_mas publish / persist."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.config import Settings, resolve_dry_run
from diagnostic_mas.case import Budget, CaseFile
from diagnostic_mas.state_paths import persist_case


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
        slack_webhook_url="https://hooks.example/x",
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


def test_persist_case_writes_report_md(tmp_path: Path):
    case = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    out = persist_case(
        tmp_path / "diagnostic_mas",
        run_id="2026-08-19T12:00:00Z",
        case=case,
        report="# Hello\n",
    )
    assert out.name == "case.json"
    assert (out.parent / "report.md").read_text(encoding="utf-8") == "# Hello\n"
    latest = (tmp_path / "diagnostic_mas" / "latest.json").read_text(encoding="utf-8")
    assert "report_path" in latest


def test_publish_flag_resolves_deliver(tmp_path: Path):
    s = _settings(state_dir=tmp_path, dry_run=True)
    assert resolve_dry_run(settings=s, publish=True) is False


def test_publish_and_dry_run_cli_conflict(tmp_path: Path):
    from diagnostic_mas.run import main

    with patch("diagnostic_mas.run.load_settings", return_value=_settings(state_dir=tmp_path)):
        with patch(
            "diagnostic_mas.run.prepare_fabric_llm", return_value=(None, True)
        ):
            with pytest.raises(ValueError, match="either --publish or --dry-run"):
                main(["--publish", "--dry-run", "--skip-llm"])


@pytest.mark.asyncio
async def test_run_publish_persists_and_publishes(tmp_path: Path):
    from diagnostic_mas import run as run_mod

    settings = _settings(state_dir=tmp_path, dry_run=True)
    args = argparse.Namespace(
        skip_llm=True,
        dry_run=False,
        publish=True,
        isis_only=False,
        bgp_only=False,
        device_only=False,
        service_only=False,
        skip_service=True,
        devices="",
        service_type="",
        service_id="",
        max_deep_checks=0,
        max_handoffs=0,
    )

    class _CM:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            return False

    with patch.object(run_mod, "load_settings", return_value=settings):
        with patch.object(run_mod, "prepare_fabric_llm", return_value=(None, True)):
            with patch.object(run_mod, "mcp_session", return_value=_CM()):
                with patch.object(
                    run_mod,
                    "call_mcp",
                    new=AsyncMock(
                        return_value={"status": "success", "data": {"device": []}}
                    ),
                ):
                    with patch.object(
                        run_mod, "parse_device_names", return_value=["renc-data-sw"]
                    ):
                        with patch.object(
                            run_mod, "run_mandatory_spines", new=AsyncMock()
                        ):
                            with patch.object(
                                run_mod,
                                "summary_narrative",
                                new=AsyncMock(return_value="summary"),
                            ):
                                with patch.object(
                                    run_mod,
                                    "render_report",
                                    return_value="# report\n",
                                ):
                                    with patch.object(
                                        run_mod, "publish_all", return_value=["slack"]
                                    ) as pa:
                                        code = await run_mod._run(args)

    assert code == 0
    assert pa.called
    runs = list((tmp_path / "diagnostic_mas" / "runs").iterdir())
    assert runs
    assert (runs[0] / "report.md").read_text(encoding="utf-8") == "# report\n"
    assert (runs[0] / "case.json").exists()
