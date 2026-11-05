"""CLI: diagnostic MAS (blackboard coordinator + role spines)."""

from __future__ import annotations

import argparse
import asyncio
import os
from copy import copy
from dataclasses import dataclass
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

import time  # noqa: E402

from agent.config import load_settings, resolve_dry_run  # noqa: E402
from agent.fabric_key import (  # noqa: E402
    get_fabric_api_key_lifetime,
    prepare_fabric_llm,
    resolve_fabric_model,
)
from agent.llm_usage import (  # noqa: E402
    begin_llm_accounting, budget_line, end_llm_accounting, read_llm_spend,
)
from agent.publish import mask_nso_address, publish_all  # noqa: E402
from diagnostic_mas.case import Budget, CaseFile  # noqa: E402
from diagnostic_mas.coordinator import (  # noqa: E402
    run_autonomous_loop,
    run_mandatory_spines,
)
from diagnostic_mas.expand import expand_spine_devices  # noqa: E402
from diagnostic_mas.focus import (  # noqa: E402
    counts_from_services,
    endpoint_devices_from_services,
    filter_device_names,
    filter_services,
    resolve_spine_flags,
)
from diagnostic_mas.device_health import services_from_case  # noqa: E402
from diagnostic_mas.metrics import metrics_snapshot_from_case  # noqa: E402
from diagnostic_mas.report import render_report  # noqa: E402
from diagnostic_mas.roles.service import issues_from_service_health  # noqa: E402
from diagnostic_mas.roles.summary import summary_narrative  # noqa: E402
from diagnostic_mas.state_paths import (  # noqa: E402
    case_to_dict,
    diagnostic_mas_state_dir,
    persist_case,
)
from nso_facts.mcp_client import (  # noqa: E402
    call_mcp,
    mcp_error_message,
    mcp_is_error,
    mcp_session,
    nso_unreachable_reason,
    nso_watch,
)
from nso_facts.mcp_accounting import (  # noqa: E402
    current_mcp_stats,
    mcp_call_record,
    print_mcp_accounting,
    set_mcp_stage,
    start_mcp_accounting,
    stop_mcp_accounting,
)
from nso_facts.metrics import push_phase1_metrics, push_scan_attempt_failed  # noqa: E402
from nso_facts.topology.devices import parse_device_names  # noqa: E402


def _refilter_service_evidence(
    case: CaseFile,
    focus: list[str],
    service_type: str | None,
    service_id: str | None,
) -> None:
    """Narrow service spine Evidence to focus devices / type / id."""
    for ev in case.evidence:
        if ev.get("kind") != "spine" or ev.get("role") != "service":
            continue
        payload = ev.get("payload")
        if not isinstance(payload, dict):
            continue
        extra = payload.get("extra")
        if not isinstance(extra, dict):
            continue
        services = filter_services(
            extra.get("services") or {},
            service_type=service_type,
            service_id=service_id,
            devices=focus,
        )
        counts = counts_from_services(services)
        extra["services"] = services
        extra["counts"] = counts
        payload["operational_summary"] = counts
        payload["static_summary"] = {"service_keys": len(services)}
        keep_ids = {
            str(i.get("edge_id") or "") for i in issues_from_service_health(services)
        }
        case.issues = [
            i
            for i in case.issues
            if i.get("layer") != "services"
            or str(i.get("edge_id") or "") in keep_ids
        ]
        existing = {
            (i.get("code"), i.get("edge_id"))
            for i in case.issues
            if i.get("layer") == "services"
        }
        from diagnostic_mas.case import open_issue

        for issue in issues_from_service_health(services):
            key = (issue.get("code"), issue.get("edge_id"))
            if key in existing:
                continue
            kwargs: dict = {
                "code": str(issue.get("code") or "service"),
                "message": str(issue.get("message") or ""),
                "evidence_ids": [str(ev.get("id") or "")],
                "layer": "services",
                "edge_id": issue.get("edge_id"),
                "devices": issue.get("devices"),
            }
            if isinstance(issue.get("live_l2"), dict):
                kwargs["live_l2"] = issue["live_l2"]
            if isinstance(issue.get("device_sync"), dict):
                kwargs["device_sync"] = issue["device_sync"]
            if "in_sync" in issue:
                kwargs["in_sync"] = issue.get("in_sync")
            if issue.get("system_status"):
                kwargs["system_status"] = issue["system_status"]
            if issue.get("dataplane_status"):
                kwargs["dataplane_status"] = issue["dataplane_status"]
            open_issue(case, **kwargs)
        break


async def _check_nso(settings) -> bool:
    """Report whether the MCP server starts and NSO answers."""
    try:
        async with mcp_session(settings) as client:
            tools = await client.list_tools()
            print(f"MCP server: OK ({len(tools)} tools)")
            try:
                result = await call_mcp(client, "list_devices")
            except Exception as exc:  # noqa: BLE001
                print(f"NSO: FAILED — {exc}", file=sys.stderr)
                return False
    except Exception as exc:  # noqa: BLE001
        print(f"MCP server: FAILED — {exc}", file=sys.stderr)
        return False
    if mcp_is_error(result):
        print(f"NSO: FAILED — {mcp_error_message(result)}", file=sys.stderr)
        return False
    devices = parse_device_names(result)
    if not devices:
        print("NSO: FAILED — no devices returned", file=sys.stderr)
        return False
    print(f"NSO: OK ({len(devices)} devices)")
    return True


def _check_llm(settings) -> bool:
    """Report whether the configured LLM key and model work; an unset key is not a failure."""
    if not settings.fabric_api_key:
        print("LLM: not configured (scans need --skip-llm)")
        return True
    lifetime = get_fabric_api_key_lifetime(settings)
    reason = (
        "key invalid or revoked" if lifetime.auth_invalid
        else "key expired" if lifetime.is_expired
        else "spend budget exceeded" if lifetime.budget_exhausted
        else None
    )
    if reason is None:
        model = resolve_fabric_model(settings)
        if model.resolved:
            print(f"LLM: OK (model {model.resolved})")
            for line in (
                lifetime.format_line() if lifetime.expires_at else None,
                lifetime.format_allowance_line(),
            ):
                if line:
                    print(f"  {line}")
            print(f"  {budget_line(read_llm_spend(settings))}")
            return True
        reason = model.detail or "model check failed"
    print(f"LLM: FAILED — {reason}", file=sys.stderr)
    return False


def _check_delivery(settings) -> bool:
    """Report whether each configured delivery channel is usable; nothing is sent."""
    from agent.publish import delivery_checks

    results = delivery_checks(settings)
    if not results:
        print("Delivery: none configured (published reports are saved only)")
        return True
    for channel, ok, detail in results:
        if ok:
            print(f"{channel}: {detail}" if detail.startswith("configured") else f"{channel}: OK ({detail})")
        else:
            print(f"{channel}: FAILED — {detail}", file=sys.stderr)
    return all(ok for _, ok, _ in results)


async def _check_connection(settings, *, check_llm: bool) -> int:
    """Pre-flight for a scan: MCP server, NSO, the LLM unless skipped, and delivery.

    No scan is run, nothing is saved and nothing is published.
    """
    ok = await _check_nso(settings)
    if check_llm:
        ok = _check_llm(settings) and ok
    ok = _check_delivery(settings) and ok
    return 0 if ok else 1


async def _run(args: argparse.Namespace) -> int:
    if getattr(args, "check_connection", False):
        return await _check_connection(
            load_settings(require_llm_key=False),
            check_llm=not getattr(args, "skip_llm", False),
        )
    # --skip-llm needs no LLM key and makes no request to the LLM endpoint.
    settings = load_settings(require_llm_key=not args.skip_llm)
    force_skip_llm = False
    if not args.skip_llm:
        # Before the first request, so the scan's cost includes the start-up probe.
        print(begin_llm_accounting(settings), file=sys.stderr)
        _, force_skip_llm = prepare_fabric_llm(settings)
    skip_llm = bool(args.skip_llm or force_skip_llm or not settings.fabric_api_key)
    dry_run = resolve_dry_run(
        settings=settings,
        publish=bool(getattr(args, "publish", False)),
        dry_run_flag=bool(args.dry_run),
    )

    devices_arg = (args.devices or "").strip()
    service_type = (args.service_type or "").strip() or None
    service_id = (args.service_id or "").strip() or None
    service_focus = bool(service_type or service_id)

    run_isis, run_bgp, run_service, run_device = resolve_spine_flags(
        isis_only=args.isis_only,
        bgp_only=args.bgp_only,
        device_only=args.device_only,
        service_only=args.service_only,
        skip_service=args.skip_service,
        service_focus=service_focus,
    )

    if run_device and not devices_arg:
        print(
            "--device-only requires --devices (e.g. --devices renc-data-sw)",
            file=sys.stderr,
        )
        return 2

    if getattr(args, "max_drills", None) is not None:
        args.max_tools_per_drill = int(args.max_drills)
    case = CaseFile(
        budget=Budget(
            max_deep_checks=max(0, args.max_deep_checks),
            max_handoffs=max(0, args.max_handoffs),
            max_drill_issues=max(0, getattr(args, "max_drill_issues", 2)),
            max_tools_per_drill=max(0, getattr(args, "max_tools_per_drill", 12)),
            max_dataplane_tools=max(0, getattr(args, "max_dataplane_tools", 40)),
            max_port_investigations=max(0, getattr(args, "max_port_investigations", 6)),
        )
    )
    from diagnostic_mas.run_configuration import capture_run_configuration
    case.run_configuration = capture_run_configuration(
        args, settings, case.budget, skip_llm=skip_llm, dry_run=dry_run
    )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    t0 = time.monotonic()
    start_mcp_accounting()
    lean_service = (
        run_service
        and not run_isis
        and not run_bgp
        and bool(service_type or service_id)
    )

    from nso_facts.mcp_archive import archive_mcp_results

    archive_dir = (diagnostic_mas_state_dir(settings) / "mcp-results"
                   if getattr(args, "save_mcp_results", False) else None)
    with archive_mcp_results(archive_dir, run_id,
                             keep_days=settings.mcp_results_keep_days) as archive_path:
        if archive_path:
            print(f"[mcp archive] Full results: {archive_path}", file=sys.stderr)
        def report_failed_attempt(reason: str, error: BaseException | None = None) -> None:
            """Let monitoring and the report's readers know a scan ended without a report (never on a dry run)."""
            if dry_run:
                return
            if (not getattr(args, "skip_metrics", False)
                    and push_scan_attempt_failed(settings, reason=reason, pipeline="diagnostic")):
                print("Pushed scan-failed signal to Pushgateway", file=sys.stderr)
            _send_failure_notice(settings, run_id, reason, error)

        try:
            plan = _ScanPlan(
                skip_llm=skip_llm, dry_run=dry_run, run_isis=run_isis, run_bgp=run_bgp,
                run_service=run_service, run_device=run_device, devices_arg=devices_arg,
                service_type=service_type, service_id=service_id, run_id=run_id, t0=t0,
                lean_service=lean_service,
            )
            code = await _stop_if_nso_unreachable(_run_after_accounting(
                args, settings=settings, case=case, plan=plan))
        except Exception as exc:
            report_failed_attempt("error", exc)
            raise
        else:
            if code == NSO_UNREACHABLE_EXIT:
                report_failed_attempt("nso_unreachable")
            return code
        finally:
            from agent.publish import mask_nso_address
            from diagnostic_mas.state_paths import run_folder

            # A saved scan keeps the per-call list beside its report, so the terminal ends on the result.
            print_mcp_accounting(stop_mcp_accounting(),
                                 calls_dir=run_folder(diagnostic_mas_state_dir(settings), run_id),
                                 mask=lambda text: mask_nso_address(text, os.environ.get("NSO_ADDRESS")))


# Exit status when the scan was abandoned because NSO stopped answering.
NSO_UNREACHABLE_EXIT = 3


def _send_failure_notice(settings, run_id: str, reason: str, error: BaseException | None) -> None:
    """One short notice through the report's channels; it never changes how the scan ends.

    It names the error's type only: an error's text can carry addresses or other details.
    """
    from agent.publish import publish_notice

    if reason == "nso_unreachable":
        text = (f"NSO diagnostic scan `{run_id}` stopped: NSO stopped answering, so there is no report "
                "this time. Run the scan again once NSO answers.")
    else:
        text = (f"NSO diagnostic scan `{run_id}` stopped with an error ({type(error).__name__}), so there "
                "is no report this time. The scan's log has the details.")
    failures: list[tuple[str, str]] = []
    try:
        channels = publish_notice(text, run_id, settings, failures=failures)
    except Exception as exc:  # noqa: BLE001
        print(f"Scan-failed notice not sent: {type(exc).__name__}", file=sys.stderr)
        return
    if channels:
        print(f"Sent scan-failed notice to: {', '.join(channels)}", file=sys.stderr)
    for channel, why in failures:
        print(f"Scan-failed notice not delivered by {channel}: {why}", file=sys.stderr)


async def _stop_if_nso_unreachable(scan) -> int:
    """Run the scan, abandoning it as soon as NSO itself stops answering.

    A scan without NSO can only collect timeouts, so nothing is reported,
    saved or published from it.
    """
    with nso_watch() as watch:
        work = asyncio.ensure_future(scan)
        stopped = asyncio.ensure_future(watch.stopped.wait())
        try:
            await asyncio.wait({work, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
        finally:
            for task in (work, stopped):
                task.cancel()
            await asyncio.gather(work, stopped, return_exceptions=True)
        if watch.reason:
            print(
                f"NSO is unreachable; scan stopped. NSO's device list request "
                f"failed: {watch.reason}\n"
                "No report was saved or published.",
                file=sys.stderr,
            )
            return NSO_UNREACHABLE_EXIT
        return work.result()


@dataclass(frozen=True)
class _ScanPlan:
    """What this scan collects and how it ends, settled in ``_run`` before collection starts."""

    skip_llm: bool
    dry_run: bool
    run_isis: bool
    run_bgp: bool
    run_service: bool
    run_device: bool
    devices_arg: str
    service_type: str | None
    service_id: str | None
    run_id: str
    t0: float
    # A --service-type/--service-id focus without routing spines.
    lean_service: bool


async def _run_after_accounting(
    args: argparse.Namespace, *, settings, case: CaseFile, plan: _ScanPlan,
) -> int:
    async with mcp_session(settings) as client:
        set_mcp_stage("focus")
        list_result = await call_mcp(client, "list_devices")
        if nso_unreachable_reason():
            return NSO_UNREACHABLE_EXIT
        all_names = parse_device_names(list_result)
        chosen = await _choose_devices(client, plan, all_names)
        if chosen is None:
            return 2
        focus, spine_devices, filter_services_to_devices = chosen
        case.focus_devices = list(focus)
        await _collect_spines(client, args, settings, case, plan, spine_devices,
                              filter_services_to_devices=filter_services_to_devices)
        if nso_unreachable_reason():
            return NSO_UNREACHABLE_EXIT
        if plan.lean_service:
            spine_devices = _narrow_to_matched_services(case, plan, spine_devices)
            if spine_devices is None:
                return 2
        if focus:
            case.device_names = list(focus)
            if plan.run_service and (plan.run_isis or plan.run_bgp):
                _refilter_service_evidence(case, focus, plan.service_type, plan.service_id)

        await _investigate(client, args, settings, case, plan, spine_devices=spine_devices, all_names=all_names)

        from nso_facts.mcp_client import successful_live_devices

        case.live_verified_devices = successful_live_devices()
        set_mcp_stage(None)

    # Collection that lost NSO is not evidence about the network: report nothing.
    if nso_unreachable_reason():
        return NSO_UNREACHABLE_EXIT

    from diagnostic_mas.ingest import ingest_quarantined_devices

    ingest_quarantined_devices(case)
    report_case, case_delta, previous_run_id, open_faults = _compare_with_previous(case, settings, plan)
    report = await _render(args, settings, case, report_case, plan,
                           case_delta=case_delta, previous_run_id=previous_run_id)
    return _save_and_deliver(args, settings, case, plan, report,
                             case_delta=case_delta, previous_run_id=previous_run_id, open_faults=open_faults)


async def _choose_devices(
    client: Any, plan: _ScanPlan, all_names: list[str],
) -> tuple[list[str], list[str], bool] | None:
    """The --devices focus, the devices to collect from, and whether service evidence is narrowed to them.

    None when --devices matches no device: the scan exits 2.
    """
    if plan.lean_service and not plan.devices_arg:
        # Service-type/id focus: still list devices so drill mcp_call
        # (exec_show / health) is gated against real names. Lean collect
        # skips HW/physical; inventory is for allowlisting only.
        print(
            f"[focus] lean service type={plan.service_type!r} id={plan.service_id!r} "
            f"devices={len(all_names)}",
            file=sys.stderr,
        )
        return [], list(all_names), False
    focus = filter_device_names(all_names, plan.devices_arg) if plan.devices_arg else []
    if plan.devices_arg and not focus:
        print(f"No devices matched --devices {plan.devices_arg!r}", file=sys.stderr)
        return None
    if not focus:
        return focus, all_names, False
    if plan.run_isis or plan.run_bgp:
        spine_devices = await expand_spine_devices(
            client, focus, all_names, expand_isis=plan.run_isis, expand_bgp=plan.run_bgp,
        )
        print(f"[focus] seeds={focus} spine_devices={spine_devices}", file=sys.stderr)
    else:
        spine_devices = list(focus)
        if plan.run_device:
            print(f"[focus] lean device devices={spine_devices}", file=sys.stderr)
    return focus, spine_devices, bool(plan.run_service)


async def _collect_spines(
    client: Any, args: argparse.Namespace, settings, case: CaseFile, plan: _ScanPlan, spine_devices: list[str],
    *, filter_services_to_devices: bool,
) -> None:
    """The fixed collection (IS-IS, BGP, services, devices), with the last published scan's leads."""
    expected_isis_edges = _load_previous_leads(case, settings, run_isis=plan.run_isis)
    set_mcp_stage("spines")
    await run_mandatory_spines(
        client,
        settings,
        case,
        spine_devices,
        run_isis=plan.run_isis,
        run_bgp=plan.run_bgp,
        run_service=plan.run_service,
        run_device=plan.run_device,
        service_type=plan.service_type,
        service_id=plan.service_id,
        filter_services_to_devices=filter_services_to_devices,
        spine_concurrent_devices=getattr(args, "spine_concurrent_devices", 1),
        max_port_counter_reads=max(0, getattr(args, "max_port_counter_reads", 400)),
        expected_isis_edges=expected_isis_edges,
    )


def _load_previous_leads(case: CaseFile, settings, *, run_isis: bool) -> Any:
    """Leads from the last published scan; with IS-IS, the adjacencies it expects.

    Its IS-IS adjacencies (one both routers now omit is Down) and its routing drill
    findings (leads for this scan's drills, never proof).
    """
    from diagnostic_mas.case_delta import load_previous_case
    from diagnostic_mas.drill import previous_findings
    from diagnostic_mas.port_counters import _saved as saved_port_counters
    from diagnostic_mas.port_investigation import previous_port_findings

    previous_case = load_previous_case(diagnostic_mas_state_dir(settings))[0]
    case.previous_findings = previous_findings(previous_case)
    case.previous_port_findings = previous_port_findings(previous_case)
    case.previous_port_counters = saved_port_counters(previous_case or {})
    if not run_isis:
        return None
    from diagnostic_mas.roles.isis import expected_adjacencies

    return expected_adjacencies(previous_case)


def _narrow_to_matched_services(case: CaseFile, plan: _ScanPlan, spine_devices: list[str]) -> list[str] | None:
    """For a service focus: the matched services' endpoint devices; None when none matched (exit 2)."""
    matched = services_from_case(case)
    if not matched:
        bits = []
        if plan.service_type:
            bits.append(f"type={plan.service_type!r}")
        if plan.service_id:
            bits.append(f"id={plan.service_id!r}")
        print(
            "No services matched "
            + " and ".join(bits)
            + " — not falling back to a broad scan.",
            file=sys.stderr,
        )
        return None
    endpoints = endpoint_devices_from_services(matched)
    case.focus_devices = list(endpoints)
    case.device_names = list(endpoints) or list(case.device_names or [])
    print(f"[focus] matched_services={len(matched)} endpoint_devices={endpoints}", file=sys.stderr)
    from diagnostic_mas.dataplane_verify import init_service_coverage

    init_service_coverage(case)
    return list(endpoints) or spine_devices


async def _investigate(
    client: Any, args: argparse.Namespace, settings, case: CaseFile, plan: _ScanPlan,
    *, spine_devices: list[str], all_names: list[str],
) -> None:
    """The LLM phases: dataplane checks, the autonomous loop, drills, and a service focus's underlay expansion."""
    if not plan.skip_llm and plan.run_service:
        await _check_dataplane(client, args, settings, case, plan,
                               device_names=set(spine_devices or all_names or case.device_names))

    # Lean service focus: skip multi-minute autonomous planner; still allow
    # budgeted drill, then optional underlay expand when evidence needs it.
    if plan.lean_service:
        print("[focus] lean service: skip autonomous (dataplane + optional drill/expand)", file=sys.stderr)
    elif _may_run_autonomous(case, plan):
        set_mcp_stage("autonomous")
        await run_autonomous_loop(client, settings, case, device_names=spine_devices or all_names)
    elif getattr(case, "llm_halt_reason", None):
        print(
            "[llm] provider budget exceeded — skipping autonomous/drill/"
            f"summary LLM ({case.llm_halt_reason})",
            file=sys.stderr,
        )

    if _may_drill(case, plan):
        from diagnostic_mas.drill import run_drill_phase

        set_mcp_stage("drill")
        await run_drill_phase(client, settings, case,
                              device_names=set(spine_devices or all_names or case.device_names),
                              skip_llm=plan.skip_llm)

    if plan.lean_service and not plan.skip_llm:
        from diagnostic_mas.service_expand import maybe_expand_underlay_for_service_focus

        set_mcp_stage("expand")
        await maybe_expand_underlay_for_service_focus(client, case, physical_edges=[])


async def _check_dataplane(
    client: Any, args: argparse.Namespace, settings, case: CaseFile, plan: _ScanPlan, *, device_names: set[str],
) -> None:
    from diagnostic_mas.dataplane_verify import init_service_coverage, run_dataplane_verify_phase

    if not plan.lean_service and not case.service_coverage:
        init_service_coverage(case)
    set_mcp_stage("dataplane")
    await run_dataplane_verify_phase(
        client,
        settings,
        case,
        device_names=device_names,
        skip_llm=plan.skip_llm,
        suspicious_only=plan.lean_service,
        explicit_service=bool(plan.service_id),
        max_services=getattr(args, "max_dataplane_services", None),
        max_per_category=getattr(args, "max_dataplane_per_category", None),
        concurrent_works=getattr(args, "dataplane_concurrent_works", 1),
        category_rotate_seed=plan.run_id,
    )


def _may_run_autonomous(case: CaseFile, plan: _ScanPlan) -> bool:
    return (not plan.skip_llm and not getattr(case, "llm_halt_reason", None)
            and (case.budget.max_deep_checks > 0 or case.budget.max_handoffs > 0))


def _may_drill(case: CaseFile, plan: _ScanPlan) -> bool:
    """Drills need the LLM, a drill budget, and, for a service focus, an open issue."""
    return (not plan.skip_llm and not getattr(case, "llm_halt_reason", None)
            and case.budget.max_drill_issues > 0 and case.budget.max_tools_per_drill > 0
            and (not plan.lean_service or any(i.get("status") == "open" for i in case.issues)))


def _compare_with_previous(
    case: CaseFile, settings, plan: _ScanPlan,
) -> tuple[CaseFile, dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    """The case the report shows, the changes since the last published scan, its run id, and the open faults.

    Compared to the last published case when there is one, even on a dry run.
    """
    from diagnostic_mas.case_delta import (
        compute_case_delta,
        first_seen_from_saved_scans,
        load_previous_case,
        service_fault_history,
    )

    state_dir = diagnostic_mas_state_dir(settings)
    previous_case, previous_run_id = load_previous_case(state_dir)
    case.last_known_service_faults = service_fault_history(
        previous_case or {}, case_to_dict(case),
        previous_run_id=previous_run_id, run_id=plan.run_id,
    )
    first_seen_from_saved_scans(case.last_known_service_faults, Path(state_dir) / "runs", before=plan.run_id)
    current = case_to_dict(case)
    case_delta = None
    if previous_case is not None:
        case_delta = compute_case_delta(previous_case, current)
        case_delta["last_known_service_faults"] = case.last_known_service_faults
    # How long routing faults, error links and IS-IS changes have been seen: saved scans only.
    from diagnostic_mas.item_history import history_for_scan

    case.item_history = history_for_scan(previous_case, current, runs_dir=Path(state_dir) / "runs",
                                         run_id=plan.run_id)
    if case_delta is not None:
        case_delta["item_history"] = case.item_history
    report_case, case_delta = _scoped_to_services(case, case_delta, plan)

    # What is still open, grouped by the port or device that explains it, with its age.
    from diagnostic_mas.open_faults import open_fault_groups

    open_faults = open_fault_groups(
        report_case, report_case.last_known_service_faults,
        new=[row.get("service") for row in (case_delta or {}).get("newly_failed_services") or []])
    if case_delta is not None:
        case_delta["open_faults"] = open_faults
    return report_case, case_delta, previous_run_id, open_faults


def _scoped_to_services(
    case: CaseFile, case_delta: dict[str, Any] | None, plan: _ScanPlan,
) -> tuple[CaseFile, dict[str, Any] | None]:
    """For a service focus, the report lists only the focused services' last-known faults.

    The case itself keeps the full fault ledger for persistence.
    """
    if not (plan.service_type or plan.service_id):
        return case, case_delta
    from diagnostic_mas.dataplane_verify import iter_service_records

    names = {str(rec.get("name") or "") for _, rec in iter_service_records(case)}
    report_case = copy(case)
    report_case.last_known_service_faults = [
        row for row in case.last_known_service_faults if row.get("service") in names
    ]
    if case_delta is not None:
        case_delta = dict(case_delta)
        case_delta["last_known_service_faults"] = report_case.last_known_service_faults
    return report_case, case_delta


async def _render(
    args: argparse.Namespace, settings, case: CaseFile, report_case: CaseFile, plan: _ScanPlan,
    *, case_delta: dict[str, Any] | None, previous_run_id: str | None,
) -> str:
    """The report, with the LLM's Summary on top when it ran, printed and returned with NSO's address masked."""
    narrative, reporting_notes = await _summary(args, settings, case, report_case, plan,
                                                case_delta=case_delta, previous_run_id=previous_run_id)
    # The MCP calls so far; the full per-call list is still printed when the run ends.
    case.mcp_calls = report_case.mcp_calls = mcp_call_record(current_mcp_stats())
    report = render_report(
        report_case,
        summary=narrative or None,
        services_detail=bool(
            getattr(args, "services_detail", False) or plan.service_type or plan.service_id
        ),
        duration_seconds=time.monotonic() - plan.t0,
        dry_run=plan.dry_run,
        reporting_notes=reporting_notes or None,
        llm_model=None if plan.skip_llm else str(settings.fabric_model or "").strip() or None,
        case_delta=case_delta,
        previous_run_id=previous_run_id,
    )
    # Error messages carry NSO's RESTCONF URLs; nothing printed or sent shows its address.
    report = mask_nso_address(report, os.environ.get("NSO_ADDRESS"))
    print(report)
    return report


async def _summary(
    args: argparse.Namespace, settings, case: CaseFile, report_case: CaseFile, plan: _ScanPlan,
    *, case_delta: dict[str, Any] | None, previous_run_id: str | None,
) -> tuple[str, list[str]]:
    """The LLM's Summary ("" without one) and the notes the report carries about it.

    The LLM writes only the top Summary; the device and service body is deterministic.
    """
    from diagnostic_mas.case_delta import compact_delta_for_llm

    narrative = ""
    reporting_notes: list[str] = []
    if getattr(case, "llm_halt_reason", None):
        reporting_notes.append(
            f"Provider LLM halted: {case.llm_halt_reason}. "
            "Completed digs preserved; further LLM requests skipped."
        )
    if plan.skip_llm:
        return narrative, reporting_notes
    try:
        narrative = await summary_narrative(
            report_case,
            settings,
            skip_llm=False,
            deterministic_summary=bool(getattr(args, "deterministic_summary", False))
            or bool(getattr(case, "llm_halt_reason", None)),
            case_delta=compact_delta_for_llm(case_delta),
            previous_run_id=previous_run_id,
        )
    except Exception as exc:  # noqa: BLE001
        reporting_notes.append(f"Final LLM summary failed: {exc}")
        narrative = ""
    # After the last request: what the scan used, and its cost when the provider reports it.
    case.llm_usage = report_case.llm_usage = end_llm_accounting(settings)
    return narrative, reporting_notes


def _save_and_deliver(
    args: argparse.Namespace, settings, case: CaseFile, plan: _ScanPlan, report: str,
    *, case_delta: dict[str, Any] | None, previous_run_id: str | None, open_faults: list[dict[str, Any]],
) -> int:
    """Save the scan, send its notice and push its metrics; 1 when a channel could not deliver.

    A dry run does none of that and says so. A channel that cannot deliver must not stop
    the others, the saved report or metrics.
    """
    if plan.dry_run:
        print(
            "[DRY RUN] not publishing (no Slack/email) and not saving "
            "case/report state — use --publish or set DRY_RUN=0 to deliver"
            + ("; MCP results saved separately (--save-mcp-results)"
               if getattr(args, "save_mcp_results", False) else ""),
            file=sys.stderr,
        )
        return 0
    from urllib.parse import quote

    from diagnostic_mas.html_report import notification_digest, report_notice
    from diagnostic_mas.open_faults import digest_changes

    state_dir = diagnostic_mas_state_dir(settings)
    nso_address = os.environ.get("NSO_ADDRESS")
    delivery_failures: list[tuple[str, str]] = []
    out = persist_case(state_dir, run_id=plan.run_id, case=case, report=report, nso_address=nso_address)
    if settings.runs_keep_days > 0:
        from diagnostic_mas.state_paths import prune_saved_runs

        prune_saved_runs(state_dir, run_id=plan.run_id, keep_days=settings.runs_keep_days)
    report_url = None
    if settings.diagnostic_report_base_url:
        report_url = (settings.diagnostic_report_base_url.rstrip("/") + "/"
                      + quote(out.parent.name, safe="") + "/report.html")
    notice = (notification_digest(report, plan.run_id, changes=list(
                  digest_changes(case_delta, previous_run_id, open_faults)))
              if settings.notification_digest else report_notice(plan.run_id))
    channels = publish_all(
        mask_nso_address(notice, nso_address),
        plan.run_id,
        settings,
        attachment_path=out.parent / "report.html",
        report_url=report_url,
        failures=delivery_failures,
    )
    print(f"\nWrote case/report under {out.parent}", file=sys.stderr)
    if channels:
        print(f"Published to: {', '.join(channels)}", file=sys.stderr)
    elif not delivery_failures:
        print("No Slack/email configured (artifacts saved only)", file=sys.stderr)
    for channel, reason in delivery_failures:
        print(f"Delivery failed: {channel} — {reason}", file=sys.stderr)
    if not bool(getattr(args, "skip_metrics", False)):
        _push_metrics(settings, case, plan, report_url=report_url, case_delta=case_delta)
    return 1 if delivery_failures else 0


def _push_metrics(
    settings, case: CaseFile, plan: _ScanPlan, *, report_url: str | None, case_delta: dict[str, Any] | None,
) -> None:
    snapshot = metrics_snapshot_from_case(
        case, run_id=plan.run_id, report_url=report_url,
        links_gaining_errors=(case_delta or {}).get("links_gaining_errors"))
    if push_phase1_metrics(
        snapshot,
        settings,
        success=True,
        duration_seconds=time.monotonic() - plan.t0,
        pipeline="diagnostic",
    ):
        print(
            f"Pushed metrics to Pushgateway "
            f"(job={settings.prometheus_job}, "
            f"instance={settings.prometheus_instance})",
            file=sys.stderr,
        )


def _positive_worker_count(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Diagnostic MAS: ISIS/BGP/Service/Device blackboard runner"
    )
    parser.add_argument(
        "--profile", metavar="FILE",
        help=("Read options from FILE, one or more per line (# starts a comment); "
              "options typed on the command line win"),
    )
    parser.add_argument(
        "--max-deep-checks",
        type=int,
        default=0,
        help=(
            "Autonomous deep-check budget (default 0 = skip; Fabric plan turns "
            "are multi-minute — opt in with e.g. --max-deep-checks 4)"
        ),
    )
    parser.add_argument(
        "--max-handoffs",
        type=int,
        default=0,
        help="Autonomous handoff budget (default 0; pair with --max-deep-checks)",
    )
    parser.add_argument(
        "--max-drill-issues",
        type=int,
        default=2,
        help="Max Issues to drill (default 2; e.g. top degraded services)",
    )
    parser.add_argument(
        "--max-tools-per-drill",
        type=int,
        default=12,
        help="Max MCP tool calls per drilled Issue (default 12)",
    )
    parser.add_argument(
        "--max-port-investigations",
        type=int,
        default=6,
        help=(
            "Max down ports and cut-off devices investigated once before the "
            "service digs (default 6; 0 investigates every service individually)"
        ),
    )
    parser.add_argument(
        "--max-port-counter-reads",
        type=int,
        default=400,
        help=(
            "Max ports whose error counters are read each scan, for comparison with the "
            "next scan: ports under a service or an IS-IS adjacency (default 400; 0 reads none)"
        ),
    )
    parser.add_argument(
        "--spine-concurrent-devices", type=_positive_worker_count, default=1,
        help="Maximum simultaneous device calls in service operational checks (default 1)",
    )
    parser.add_argument(
        "--dataplane-concurrent-workers", dest="dataplane_concurrent_works", metavar="N",
        type=_positive_worker_count, default=1,
        help="Concurrent service dataplane digs (default 1); overlapping endpoints wait",
    )
    # The old spelling, still accepted so existing commands and scripts keep working.
    parser.add_argument(
        "--dataplane-concurrent_works", dest="dataplane_concurrent_works",
        type=_positive_worker_count, default=argparse.SUPPRESS, help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--max-dataplane-tools",
        type=int,
        default=40,
        help=(
            "Max MCP tool calls per service in dataplane LLM verify "
            "(default 40; 0 disables dataplane verify)"
        ),
    )
    parser.add_argument(
        "--max-dataplane-services",
        type=int,
        default=10,
        help=(
            "Cap how many services get a dataplane LLM investigation in total, "
            "whatever their type (default 10; 0 turns them off). Services are "
            "taken in turns across types, most suspicious first; "
            "--max-dataplane-per-category also limits each type. Soft-error "
            "live L2 is preferred within the cap, not added beyond it."
        ),
    )
    parser.add_argument(
        "--max-dataplane-per-category",
        type=int,
        default=None,
        help=(
            "Take up to N dataplane LLM digs from each typed prompt category "
            "(l2ptp / l2sts / l3rt, …). Use for even overnight samples "
            "(e.g. --max-dataplane-per-category 10). Optional "
            "--max-dataplane-services still truncates the combined list."
        ),
    )
    parser.add_argument(
        "--max-drills",
        type=int,
        default=None,
        help="Deprecated alias for --max-tools-per-drill",
    )
    parser.add_argument(
        "--save-mcp-results",
        action="store_true",
        help="Archive complete MCP responses locally, including during dry runs; no publishing",
    )
    parser.add_argument(
        "--configure",
        action="store_true",
        help=(
            "Ask for the NSO, LLM, Slack, email and Pushgateway settings and write them "
            "into .env in the current directory, then offer --check-connection; Enter "
            "keeps a current value"
        ),
    )
    parser.add_argument(
        "--check-connection",
        action="store_true",
        help=(
            "Check the MCP server, NSO, (unless --skip-llm) the LLM key and model, and the "
            "configured Slack/email delivery, then exit; no scan or publishing"
        ),
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="MCP spines only; skip autonomous diagnosis (no LLM model)",
    )
    parser.add_argument(
        "--deterministic-summary",
        action="store_true",
        help=(
            "Skip the final FABRIC Summary chat; restate dataplane "
            "diagnoses/findings instead (default: LLM Summary)"
        ),
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Send Slack/email and save state/diagnostic_mas/ (overrides DRY_RUN=1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=("No Slack/email, saved state or metrics push (default when DRY_RUN=1). "
              "Still reads live NSO and devices, and calls the LLM unless --skip-llm"),
    )
    parser.add_argument(
        "--services-detail",
        action="store_true",
        help=(
            "Include per-instance service sections for every matched service "
            "(default: category table + incomplete checks grouped by "
            "endpoint/reason; only digs and confirmed impairments get "
            "per-instance detail). Auto-enabled with --service-type/--service-id"
        ),
    )
    parser.add_argument(
        "--skip-metrics",
        action="store_true",
        help="Do not push Phase 1 gauges to Prometheus Pushgateway",
    )
    parser.add_argument(
        "--devices",
        type=str,
        default="",
        help=(
            "Focus on these devices (exact or substring). ISIS/BGP collect the "
            "seed(s) plus one-hop peers only — not the full inventory."
        ),
    )
    parser.add_argument(
        "--service-type",
        type=str,
        default="",
        help=(
            "Service-first focus by type (e.g. l2ptp). Implies lean service-only: "
            "match instances, basic checks for all, prioritize suspicious for LLM; "
            "no whole-NSO topology scan unless evidence expands"
        ),
    )
    parser.add_argument(
        "--service-id",
        type=str,
        default="",
        help=(
            "Service-first focus by instance name/id substring. Implies lean "
            "service-only (same as --service-type). Combined with --service-type "
            "as an intersection; exit if nothing matches. With LLM enabled, "
            "investigate even when basic checks pass (within budget)"
        ),
    )
    parser.add_argument(
        "--skip-service",
        action="store_true",
        help="Skip Service/fleet spine (ignored by --device-only/--service-only)",
    )
    layer = parser.add_mutually_exclusive_group()
    layer.add_argument("--isis-only", action="store_true")
    layer.add_argument("--bgp-only", action="store_true")
    layer.add_argument(
        "--device-only",
        action="store_true",
        help="Physical skipped; fleet sync + HW/system for --devices only",
    )
    layer.add_argument(
        "--service-only",
        action="store_true",
        help="Service focus; with --service-type lean MCP (no full NSO walk)",
    )
    return parser


# Options that set the same thing or cannot be combined: one typed on the command line
# replaces any of them in the profile.
_REPLACES_TOGETHER = (
    frozenset({"publish", "dry_run"}),
    frozenset({"isis_only", "bgp_only", "device_only", "service_only"}),
    frozenset({"max_drills", "max_tools_per_drill"}),
)


def _read_profile(parser: argparse.ArgumentParser, path: str) -> list[list[str]]:
    """A profile's options, each with the values that follow it; stops on a line it cannot use."""
    import shlex

    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        parser.error(f"cannot read profile {path}: {getattr(exc, 'strerror', None) or exc}")
    known = parser._option_string_actions
    items: list[list[str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        where = f"profile {path} line {number}"
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError as exc:
            parser.error(f"{where}: {exc}")
        if tokens and not tokens[0].startswith("--"):
            parser.error(f"{where}: expected an option, found {tokens[0]!r}")
        for token in tokens:
            if not token.startswith("--"):
                items[-1].append(token)
                continue
            option = token.split("=", 1)[0]
            if option not in known:
                parser.error(f"{where}: unknown option {option}")
            if known[option].dest == "profile":
                parser.error(f"{where}: a profile cannot name another profile")
            items.append([token])
    return items


def parse_cli(argv: list[str] | None = None) -> argparse.Namespace:
    """The command line's options, after those of its --profile file, so typed options win.

    ``args.profile_options`` lists the profile's options as written, marking any the
    command line replaced, for the report's run configuration.
    """
    import shlex

    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    paths = [arg.split("=", 1)[1] if arg.startswith("--profile=") else argv[index + 1]
             for index, arg in enumerate(argv)
             if arg.startswith("--profile=") or (arg == "--profile" and index + 1 < len(argv))]
    if len(paths) > 1:
        parser.error("give --profile only once")
    profile_options: list[str] = []
    if paths:
        known = parser._option_string_actions
        typed = {known[arg.split("=", 1)[0]].dest for arg in argv
                 if arg.startswith("--") and arg.split("=", 1)[0] in known}
        replaced = typed.union(*(group for group in _REPLACES_TOGETHER if typed & group))
        kept: list[str] = []
        for item in _read_profile(parser, paths[0]):
            if known[item[0].split("=", 1)[0]].dest in replaced:
                profile_options.append(shlex.join(item) + " (replaced on the command line)")
            else:
                profile_options.append(shlex.join(item))
                kept.extend(item)
        argv = kept + argv
    args = parser.parse_args(argv)
    if args.profile is not None and not paths:
        parser.error("write --profile in full")
    args.profile_options = profile_options
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_cli(argv)
    # --max-drills is a deprecated alias for --max-tools-per-drill
    if getattr(args, "max_drills", None) is not None:
        args.max_tools_per_drill = int(args.max_drills)
    if args.configure:
        from agent.configure import configure_from_cli

        return configure_from_cli()
    from agent.config import SettingsError

    try:
        return asyncio.run(_run(args))
    except SettingsError as exc:
        print(f"Settings: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
