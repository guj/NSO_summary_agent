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
@pytest.mark.parametrize("digest", [False, True])
async def test_run_publish_persists_and_publishes(tmp_path: Path, digest: bool):
    from dataclasses import replace

    from diagnostic_mas import run as run_mod

    settings = replace(_settings(state_dir=tmp_path, dry_run=True), notification_digest=digest)
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
    # Slack and email carry the HTML report with one line naming the run, unless the digest is asked for.
    sent = pa.call_args.args[0]
    if digest:
        assert sent.startswith(f"# NSO diagnostic report — {runs[0].name}\n")
    else:
        assert sent == f"NSO diagnostic report — {runs[0].name}"
    assert pa.call_args.kwargs["attachment_path"] == runs[0] / "report.html"


# A documentation address stands in for the NSO address, which must not appear in the repo.
NSO = "192.0.2.146"


def test_nso_address_is_masked_keeping_its_last_two_characters():
    from agent.publish import mask_nso_address

    text = (f"brist-data-sw: RESTCONF 500 at https://{NSO}:443/restconf/data/tailf-ncs:devices/device=x; "
            f"peers 192.0.2.14 and 10.148.0.1; again {NSO}.")
    assert mask_nso_address(text, NSO) == (
        "brist-data-sw: RESTCONF 500 at https://xxx.x.x.x46:443/restconf/data/tailf-ncs:devices/device=x; "
        "peers 192.0.2.14 and 10.148.0.1; again xxx.x.x.x46.")
    assert mask_nso_address(text, None) == text and mask_nso_address(text, "") == text
    assert mask_nso_address("at https://nso.lab.example:443/", "nso.lab.example") == "at https://xxx.xxx.xxxxxle:443/"


def test_saved_reports_mask_the_nso_address_and_the_case_keeps_it(tmp_path: Path):
    case = CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1))
    case.issues = [{"code": "collection_error", "layer": "routing", "status": "open",
                    "message": f"brist-data-sw: RESTCONF 500 at https://{NSO}:443/restconf/data/x"}]
    out = persist_case(tmp_path / "diagnostic_mas", run_id="r1", case=case,
                       report=f"# Report\n\nRESTCONF 500 at https://{NSO}:443/restconf\n", nso_address=NSO)

    for name in ("report.md", "report.html"):
        text = (out.parent / name).read_text(encoding="utf-8")
        assert NSO not in text and "xxx.x.x.x46" in text, name
    # Local state only: the next scan compares problems by their exact message.
    assert NSO in out.read_text(encoding="utf-8")


def test_an_already_masked_report_renders_intact_in_html(tmp_path: Path):
    # The runner masks the report before it is saved, so the HTML is rendered from masked Markdown.
    from agent.publish import mask_nso_address

    report = mask_nso_address(f"# Report\n\n- RESTCONF 500 at https://{NSO}:443/restconf\n", NSO)
    out = persist_case(tmp_path / "diagnostic_mas", run_id="r1", case=CaseFile(budget=Budget(max_deep_checks=1, max_handoffs=1)),
                       report=report, nso_address=NSO)

    html = (out.parent / "report.html").read_text(encoding="utf-8")
    assert "https://xxx.x.x.x46:443/restconf" in html and NSO not in html


@pytest.mark.asyncio
async def test_run_saves_how_long_a_routing_fault_has_been_seen(tmp_path: Path):
    import json
    from contextlib import ExitStack

    from diagnostic_mas import run as run_mod

    edge = "bgp:10.0.0.1:10.0.1.1:renc-data-sw:lbnl-data-sw"

    async def spines(_client, _settings, case, *_args, **_kwargs):
        case.issues.append({"code": "configured_no_session", "edge_id": edge, "layer": "routing", "status": "open"})

    args = argparse.Namespace(
        skip_llm=True, dry_run=False, publish=True, isis_only=False, bgp_only=False, device_only=False,
        service_only=False, skip_service=True, devices="", service_type="", service_id="",
        max_deep_checks=0, max_handoffs=0)

    class _CM:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            return False

    with ExitStack() as stack:
        for name, value in (("load_settings", MagicMock(return_value=_settings(state_dir=tmp_path))),
                            ("prepare_fabric_llm", MagicMock(return_value=(None, True))),
                            ("mcp_session", MagicMock(return_value=_CM())),
                            ("call_mcp", AsyncMock(return_value={"status": "success", "data": {"device": []}})),
                            ("parse_device_names", MagicMock(return_value=["renc-data-sw"])),
                            ("run_mandatory_spines", AsyncMock(side_effect=spines)),
                            ("render_report", MagicMock(return_value="# report\n")),
                            ("publish_all", MagicMock(return_value=[]))):
            stack.enter_context(patch.object(run_mod, name, new=value))
        assert await run_mod._run(args) == 0

    saved = json.loads(next((tmp_path / "diagnostic_mas").rglob("case.json")).read_text(encoding="utf-8"))
    assert saved["item_history"][f"routing|{edge}"]["seen"] == 1


def _saved_run(runs: Path, name: str, *extra: str) -> Path:
    folder = runs / name
    folder.mkdir(parents=True)
    for file in ("case.json", "report.md", "report.html", *extra):
        (folder / file).write_text("x", encoding="utf-8")
    return folder


def test_old_saved_scans_are_removed_and_the_rest_kept(tmp_path: Path, capsys):
    import json

    from diagnostic_mas.state_paths import prune_saved_runs

    runs = tmp_path / "runs"
    old = _saved_run(runs, "20261001T000000Z")
    noted = _saved_run(runs, "20261002T000000Z", "notes.txt")        # something the user added
    latest = _saved_run(runs, "20261003T000000Z")                    # the next scan's comparison
    renamed = _saved_run(runs, "20261004T000000Z-keep")              # renamed to keep
    recent = _saved_run(runs, "20261025T000000Z")
    now = _saved_run(runs, "20261103T041450Z")
    (runs / "20261005T000000Z").symlink_to(old)
    (tmp_path / "latest.json").write_text(json.dumps({"case_path": str(latest / "case.json")}), encoding="utf-8")

    removed = prune_saved_runs(tmp_path, run_id="20261103T041450Z", keep_days=30)

    assert removed == 2
    assert not old.exists()
    assert sorted(p.name for p in noted.iterdir()) == ["notes.txt"]
    for kept in (latest, renamed, recent, now):
        assert (kept / "case.json").is_file()
    assert (runs / "20261005T000000Z").is_symlink()
    assert "Removed 2 saved scans older than 30 days" in capsys.readouterr().err


def test_an_old_scans_call_list_goes_with_it(tmp_path: Path):
    from diagnostic_mas.state_paths import prune_saved_runs

    old = _saved_run(tmp_path / "runs", "20261001T000000Z", "mcp_calls.txt")

    assert prune_saved_runs(tmp_path, run_id="20261103T041450Z", keep_days=30) == 1
    assert not old.exists()


def test_keep_days_zero_removes_nothing_and_a_failure_does_not_stop_the_scan(tmp_path: Path, monkeypatch, capsys):
    from diagnostic_mas.state_paths import prune_saved_runs

    runs = tmp_path / "runs"
    old = _saved_run(runs, "20261001T000000Z")
    assert prune_saved_runs(tmp_path, run_id="20261103T041450Z", keep_days=0) == 0
    assert (old / "case.json").is_file()

    def refuse(self, *args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", refuse)
    assert prune_saved_runs(tmp_path, run_id="20261103T041450Z", keep_days=30) == 0
    assert "could not remove" in capsys.readouterr().err
    assert prune_saved_runs(tmp_path / "missing", run_id="20261103T041450Z", keep_days=30) == 0


@pytest.mark.asyncio
async def test_a_saved_scan_prunes_old_scans_when_runs_keep_days_is_set(tmp_path: Path):
    from contextlib import ExitStack
    from dataclasses import replace

    from diagnostic_mas import run as run_mod

    old = _saved_run(tmp_path / "diagnostic_mas" / "runs", "20200101T000000Z")
    args = argparse.Namespace(
        skip_llm=True, dry_run=False, publish=True, isis_only=False, bgp_only=False, device_only=False,
        service_only=False, skip_service=True, devices="", service_type="", service_id="",
        max_deep_checks=0, max_handoffs=0)

    class _CM:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            return False

    settings = replace(_settings(state_dir=tmp_path), runs_keep_days=30)
    with ExitStack() as stack:
        for name, value in (("load_settings", MagicMock(return_value=settings)),
                            ("prepare_fabric_llm", MagicMock(return_value=(None, True))),
                            ("mcp_session", MagicMock(return_value=_CM())),
                            ("call_mcp", AsyncMock(return_value={"status": "success", "data": {"device": []}})),
                            ("parse_device_names", MagicMock(return_value=["renc-data-sw"])),
                            ("run_mandatory_spines", AsyncMock()),
                            ("render_report", MagicMock(return_value="# report\n")),
                            ("publish_all", MagicMock(return_value=[]))):
            stack.enter_context(patch.object(run_mod, name, new=value))
        assert await run_mod._run(args) == 0

    assert not old.exists()
    assert len(list((tmp_path / "diagnostic_mas" / "runs").iterdir())) == 1
