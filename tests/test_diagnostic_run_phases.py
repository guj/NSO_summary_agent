"""The scan after accounting starts: which phases run, in what order, on which devices.

Each test calls ``_run_after_accounting`` with one combination of focus and LLM
settings. The MCP session and every phase are stand-ins that note their calls,
so nothing contacts NSO, an LLM, Slack or Pushgateway.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent.config import Settings
from diagnostic_mas import run as run_mod
from diagnostic_mas.case import Budget, CaseFile

DEVICES = ["amst-data-sw", "dall-data-sw", "renc-data-sw"]
RUN_ID = "20261105T000000Z"


def _settings(state_dir: Path, **changes) -> Settings:
    settings = Settings(
        mcp_server_cmd="cmd", mcp_server_args=[], mcp_env={}, fabric_api_key="sk-test",
        fabric_api_url="https://llm.invalid", fabric_model="m", state_dir=state_dir, dry_run=True,
        ignore_service_types=frozenset(), max_service_types=10, slack_webhook_url=None, smtp_host=None,
        smtp_port=587, smtp_user=None, smtp_password=None, smtp_use_tls=True, email_from=None, email_to=[],
        email_subject_prefix="NSO", prometheus_pushgateway_url=None, prometheus_job="nso-summary",
        prometheus_instance="default", prompts_dir=Path("/tmp/prompts"), interface_equivalences_file=None,
        iface_troubleshoot_max_tool_rounds=10, iface_troubleshoot_disable=False)
    return replace(settings, **changes)


def _service(name: str, devices: list[str]) -> dict:
    return {"name": name, "service_type": "l2sts", "devices": devices, "system_status": "up",
            "dataplane_status": "not_checked"}


class Phases:
    """Stand-ins for the MCP session and the scan's phases; ``calls`` lists them in order."""

    def __init__(self, monkeypatch, *, services=None, issues=None, halt=None):
        import diagnostic_mas.dataplane_verify as dataplane
        import diagnostic_mas.drill as drill
        import diagnostic_mas.service_expand as service_expand

        self.calls: list[tuple[str, dict]] = []
        calls = self.calls

        class Session:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False

        def note(name):
            async def phase(*args, **kwargs):
                calls.append((name, kwargs))
                if name == "dataplane" and halt:
                    args[2].llm_halt_reason = halt
            return phase

        async def expand(client, focus, all_names, *, expand_isis, expand_bgp):
            calls.append(("expand_spine_devices", {"focus": list(focus), "isis": expand_isis, "bgp": expand_bgp}))
            return [*focus, "renc-data-sw"]

        async def spines(client, settings, case, spine_devices, **kwargs):
            calls.append(("spines", {"devices": list(spine_devices), **kwargs}))
            if services is not None:
                case.evidence.append({"kind": "spine", "role": "service", "layer": "services", "payload": {
                    "extra": {"services": {f"l2sts/{s['name']}": s for s in services}}}})
            case.issues.extend(issues or [])

        def render(case, **kwargs):
            calls.append(("render_report", {"case": case, **kwargs}))
            return "# report\n"

        def publish(notice, run_id, settings, **kwargs):
            calls.append(("publish_all", kwargs))
            return ["slack"]

        monkeypatch.setattr(run_mod, "mcp_session", lambda settings: Session())
        monkeypatch.setattr(run_mod, "call_mcp", AsyncMock(return_value={}))
        monkeypatch.setattr(run_mod, "parse_device_names", lambda result: list(DEVICES))
        monkeypatch.setattr(run_mod, "expand_spine_devices", expand)
        monkeypatch.setattr(run_mod, "run_mandatory_spines", spines)
        monkeypatch.setattr(run_mod, "_refilter_service_evidence",
                            lambda case, focus, service_type, service_id: calls.append(("refilter", {"focus": focus})))
        monkeypatch.setattr(dataplane, "run_dataplane_verify_phase", note("dataplane"))
        monkeypatch.setattr(run_mod, "run_autonomous_loop", note("autonomous"))
        monkeypatch.setattr(drill, "run_drill_phase", note("drill"))
        monkeypatch.setattr(service_expand, "maybe_expand_underlay_for_service_focus", note("expand_underlay"))
        monkeypatch.setattr(run_mod, "summary_narrative", AsyncMock(return_value="Summary."))
        monkeypatch.setattr(run_mod, "end_llm_accounting", lambda settings: {"requests": 0})
        monkeypatch.setattr(run_mod, "render_report", render)
        monkeypatch.setattr(run_mod, "publish_all", publish)
        monkeypatch.setattr(run_mod, "push_phase1_metrics", lambda *args, **kwargs: True)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def of(self, name: str) -> dict:
        return next(kwargs for called, kwargs in self.calls if called == name)


async def _scan(tmp_path, *, args=None, settings=None, budget=None, **flags):
    case = CaseFile(budget=budget or Budget(max_deep_checks=0, max_handoffs=0))
    plan = dict(skip_llm=True, dry_run=True, run_isis=False, run_bgp=False, run_service=False, run_device=False,
                devices_arg="", service_type=None, service_id=None, run_id=RUN_ID, t0=time.monotonic(),
                lean_service=False)
    plan.update(flags)
    code = await run_mod._run_after_accounting(
        args or argparse.Namespace(), settings=settings or _settings(tmp_path), case=case,
        plan=run_mod._ScanPlan(**plan))
    return code, case


LLM_BUDGET = Budget(max_deep_checks=1, max_handoffs=0, max_drill_issues=1, max_tools_per_drill=4)


@pytest.mark.asyncio
async def test_nso_lost_at_the_device_list_stops_the_scan(tmp_path, monkeypatch):
    phases = Phases(monkeypatch)
    monkeypatch.setattr(run_mod, "nso_unreachable_reason", lambda: "connection refused")

    code, _case = await _scan(tmp_path)

    assert code == run_mod.NSO_UNREACHABLE_EXIT and phases.names() == []


@pytest.mark.asyncio
async def test_nso_lost_during_collection_ends_the_scan_without_a_report(tmp_path, monkeypatch):
    phases = Phases(monkeypatch)
    answers = iter([None, None, "connection refused"])
    monkeypatch.setattr(run_mod, "nso_unreachable_reason", lambda: next(answers))

    code, _case = await _scan(tmp_path)

    assert code == run_mod.NSO_UNREACHABLE_EXIT
    assert "spines" in phases.names() and "render_report" not in phases.names()


@pytest.mark.asyncio
async def test_devices_that_match_nothing_stop_the_scan(tmp_path, monkeypatch, capsys):
    phases = Phases(monkeypatch)

    code, _case = await _scan(tmp_path, devices_arg="zzz")

    assert code == 2 and phases.names() == []
    assert "No devices matched --devices 'zzz'" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_routing_focus_expands_its_seed_devices(tmp_path, monkeypatch, capsys):
    phases = Phases(monkeypatch)

    _code, case = await _scan(tmp_path, devices_arg="amst-data-sw", run_isis=True)

    assert phases.of("expand_spine_devices") == {"focus": ["amst-data-sw"], "isis": True, "bgp": False}
    assert phases.of("spines")["devices"] == ["amst-data-sw", "renc-data-sw"]
    assert phases.of("spines")["filter_services_to_devices"] is False
    assert case.focus_devices == ["amst-data-sw"] and case.device_names == ["amst-data-sw"]
    assert "[focus] seeds=['amst-data-sw'] spine_devices=['amst-data-sw', 'renc-data-sw']" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_device_focus_collects_only_the_named_devices(tmp_path, monkeypatch, capsys):
    phases = Phases(monkeypatch)

    await _scan(tmp_path, devices_arg="dall-data-sw", run_device=True)

    assert "expand_spine_devices" not in phases.names()
    assert phases.of("spines")["devices"] == ["dall-data-sw"]
    assert "[focus] lean device devices=['dall-data-sw']" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_focus_with_routing_and_services_narrows_the_service_evidence_to_it(tmp_path, monkeypatch):
    phases = Phases(monkeypatch)

    await _scan(tmp_path, devices_arg="amst-data-sw", run_bgp=True, run_service=True)

    assert phases.of("spines")["filter_services_to_devices"] is True
    assert phases.of("refilter") == {"focus": ["amst-data-sw"]}


@pytest.mark.asyncio
async def test_a_service_focus_collects_everywhere_then_narrows_to_its_endpoints(tmp_path, monkeypatch, capsys):
    phases = Phases(monkeypatch, services=[_service("net1", ["dall-data-sw", "renc-data-sw"])])

    code, case = await _scan(tmp_path, run_service=True, service_type="l2sts", lean_service=True)

    assert code == 0
    assert phases.of("spines")["devices"] == DEVICES
    assert phases.of("spines")["filter_services_to_devices"] is False
    assert case.focus_devices == case.device_names == ["dall-data-sw", "renc-data-sw"]
    assert "net1" in case.service_coverage
    err = capsys.readouterr().err
    assert "[focus] lean service type='l2sts' id=None devices=3" in err
    assert "[focus] matched_services=1 endpoint_devices=['dall-data-sw', 'renc-data-sw']" in err


@pytest.mark.asyncio
async def test_a_service_focus_that_matches_nothing_stops_without_a_broad_scan(tmp_path, monkeypatch, capsys):
    phases = Phases(monkeypatch, services=[])

    code, _case = await _scan(tmp_path, run_service=True, service_type="l2sts", service_id="net9",
                              lean_service=True)

    assert code == 2 and "render_report" not in phases.names()
    assert ("No services matched type='l2sts' and id='net9' — not falling back to a broad scan."
            in capsys.readouterr().err)


@pytest.mark.asyncio
async def test_with_the_llm_the_dataplane_autonomous_and_drill_phases_run_in_that_order(tmp_path, monkeypatch):
    phases = Phases(monkeypatch)
    args = argparse.Namespace(max_dataplane_services=7, max_dataplane_per_category=3, dataplane_concurrent_works=2)

    code, case = await _scan(tmp_path, args=args, budget=LLM_BUDGET, skip_llm=False, run_service=True)

    assert code == 0
    assert phases.names()[:5] == ["spines", "dataplane", "autonomous", "drill", "render_report"]
    dataplane = phases.of("dataplane")
    assert dataplane["device_names"] == set(DEVICES)
    assert (dataplane["suspicious_only"], dataplane["explicit_service"]) == (False, False)
    assert (dataplane["max_services"], dataplane["max_per_category"], dataplane["concurrent_works"]) == (7, 3, 2)
    assert dataplane["category_rotate_seed"] == RUN_ID
    assert phases.of("autonomous")["device_names"] == DEVICES
    assert case.llm_usage == {"requests": 0}


@pytest.mark.asyncio
async def test_a_service_focus_with_the_llm_skips_the_autonomous_loop_but_drills_and_expands(
        tmp_path, monkeypatch, capsys):
    phases = Phases(monkeypatch, services=[_service("net1", ["dall-data-sw"])],
                    issues=[{"code": "service_down", "status": "open", "edge_id": "net1"}])

    await _scan(tmp_path, budget=LLM_BUDGET, skip_llm=False, run_service=True, service_type="l2sts",
                service_id="net1", lean_service=True)

    assert phases.names()[:4] == ["spines", "dataplane", "drill", "expand_underlay"]
    assert (phases.of("dataplane")["suspicious_only"], phases.of("dataplane")["explicit_service"]) == (True, True)
    assert "[focus] lean service: skip autonomous (dataplane + optional drill/expand)" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_service_focus_with_no_open_issue_does_not_drill(tmp_path, monkeypatch):
    phases = Phases(monkeypatch, services=[_service("net1", ["dall-data-sw"])])

    await _scan(tmp_path, budget=LLM_BUDGET, skip_llm=False, run_service=True, service_type="l2sts",
                lean_service=True)

    assert "drill" not in phases.names() and "expand_underlay" in phases.names()


@pytest.mark.asyncio
async def test_a_provider_budget_halt_skips_later_llm_phases_and_is_noted_in_the_report(
        tmp_path, monkeypatch, capsys):
    phases = Phases(monkeypatch, halt="budget_exceeded: over budget")

    await _scan(tmp_path, budget=LLM_BUDGET, skip_llm=False, run_service=True)

    assert "autonomous" not in phases.names() and "drill" not in phases.names()
    assert "[llm] provider budget exceeded — skipping autonomous/drill/summary LLM" in capsys.readouterr().err
    assert phases.of("render_report")["reporting_notes"] == [
        "Provider LLM halted: its spend limit was reached. Completed digs preserved; further LLM requests skipped."]
    assert run_mod.summary_narrative.await_args.kwargs["deterministic_summary"] is True


@pytest.mark.asyncio
async def test_a_failed_summary_is_noted_in_the_report(tmp_path, monkeypatch):
    phases = Phases(monkeypatch)
    monkeypatch.setattr(run_mod, "summary_narrative", AsyncMock(side_effect=RuntimeError("bad gateway")))

    await _scan(tmp_path, skip_llm=False)

    render = phases.of("render_report")
    assert render["summary"] is None
    assert render["reporting_notes"] == ["Final LLM summary failed: provider runtime error"]


@pytest.mark.asyncio
async def test_a_service_focused_report_lists_only_that_services_last_known_faults(tmp_path, monkeypatch):
    import diagnostic_mas.case_delta as case_delta

    phases = Phases(monkeypatch, services=[_service("net1", ["dall-data-sw"])])
    faults = [{"service": name, "status": "down", "rechecked": False} for name in ("net1", "elsewhere")]
    monkeypatch.setattr(case_delta, "service_fault_history", lambda *args, **kwargs: [dict(f) for f in faults])

    _code, case = await _scan(tmp_path, run_service=True, service_type="l2sts", lean_service=True)

    assert [row["service"] for row in case.last_known_service_faults] == ["net1", "elsewhere"]
    report_case = phases.of("render_report")["case"]
    assert [row["service"] for row in report_case.last_known_service_faults] == ["net1"]
    assert phases.of("render_report")["services_detail"] is True


@pytest.mark.asyncio
async def test_a_published_scan_links_its_report_and_says_metrics_were_pushed(tmp_path, monkeypatch, capsys):
    phases = Phases(monkeypatch)
    settings = _settings(tmp_path, diagnostic_report_base_url="https://reports.example/runs/")

    code, _case = await _scan(tmp_path, settings=settings, dry_run=False)

    assert code == 0
    assert phases.of("publish_all")["report_url"] == f"https://reports.example/runs/{RUN_ID}/report.html"
    err = capsys.readouterr().err
    assert "Published to: slack" in err
    assert "Pushed metrics to Pushgateway (job=nso-summary, instance=default)" in err


@pytest.mark.asyncio
async def test_a_dry_run_that_saved_mcp_results_says_where_they_are(tmp_path, monkeypatch, capsys):
    Phases(monkeypatch)

    await _scan(tmp_path, args=argparse.Namespace(save_mcp_results=True))

    assert ("[DRY RUN] not publishing (no Slack/email) and not saving case/report state — use --publish or set "
            "DRY_RUN=0 to deliver; MCP results saved separately (--save-mcp-results)") in capsys.readouterr().err


@pytest.mark.asyncio
async def test_the_report_gets_a_delta_with_exactly_the_keys_written_down(tmp_path, monkeypatch):
    import diagnostic_mas.case_delta as case_delta

    phases = Phases(monkeypatch)
    monkeypatch.setattr(case_delta, "load_previous_case", lambda state_dir: ({"evidence": [], "issues": []}, "r0"))

    await _scan(tmp_path)

    assert set(phases.of("render_report")["case_delta"]) == set(case_delta.CaseDelta.__annotations__)


@pytest.mark.asyncio
async def test_the_changes_section_of_a_service_focused_report_lists_only_its_faults(tmp_path, monkeypatch):
    import diagnostic_mas.case_delta as case_delta

    phases = Phases(monkeypatch, services=[_service("net1", ["dall-data-sw"])])
    faults = [{"service": name, "status": "down", "rechecked": False} for name in ("net1", "elsewhere")]
    monkeypatch.setattr(case_delta, "service_fault_history", lambda *args, **kwargs: [dict(f) for f in faults])
    monkeypatch.setattr(case_delta, "load_previous_case", lambda state_dir: ({"evidence": [], "issues": []}, "r0"))

    await _scan(tmp_path, run_service=True, service_type="l2sts", lean_service=True)

    render = phases.of("render_report")
    assert render["previous_run_id"] == "r0"
    assert [row["service"] for row in render["case_delta"]["last_known_service_faults"]] == ["net1"]
