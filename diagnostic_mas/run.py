"""CLI: diagnostic MAS (blackboard coordinator + role spines)."""

from __future__ import annotations

import argparse
import asyncio
from copy import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

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
from agent.publish import publish_all  # noqa: E402
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
    with archive_mcp_results(archive_dir, run_id) as archive_path:
        if archive_path:
            print(f"[mcp archive] Full results: {archive_path}", file=sys.stderr)
        def report_failed_attempt(reason: str) -> None:
            """Let monitoring see a scan that ended without a report (never on a dry run)."""
            if dry_run or bool(getattr(args, "skip_metrics", False)):
                return
            if push_scan_attempt_failed(settings, reason=reason, pipeline="diagnostic"):
                print("Pushed scan-failed signal to Pushgateway", file=sys.stderr)

        try:
            code = await _stop_if_nso_unreachable(_run_after_accounting(
                args,
                settings=settings,
                skip_llm=skip_llm,
                dry_run=dry_run,
                run_isis=run_isis,
                run_bgp=run_bgp,
                run_service=run_service,
                run_device=run_device,
                devices_arg=devices_arg,
                service_type=service_type,
                service_id=service_id,
                case=case,
                run_id=run_id,
                t0=t0,
                lean_service=lean_service,
            ))
        except Exception:
            report_failed_attempt("error")
            raise
        else:
            if code == NSO_UNREACHABLE_EXIT:
                report_failed_attempt("nso_unreachable")
            return code
        finally:
            print_mcp_accounting(stop_mcp_accounting())


# Exit status when the scan was abandoned because NSO stopped answering.
NSO_UNREACHABLE_EXIT = 3


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


async def _run_after_accounting(
    args: argparse.Namespace,
    *,
    settings,
    skip_llm: bool,
    dry_run: bool,
    run_isis: bool,
    run_bgp: bool,
    run_service: bool,
    run_device: bool,
    devices_arg: str,
    service_type: str | None,
    service_id: str | None,
    case: CaseFile,
    run_id: str,
    t0: float,
    lean_service: bool,
) -> int:
    async with mcp_session(settings) as client:
        # lean_service already computed above (also used after MCP session)

        set_mcp_stage("focus")
        list_result = await call_mcp(client, "list_devices")
        if nso_unreachable_reason():
            return NSO_UNREACHABLE_EXIT
        all_names = parse_device_names(list_result)
        if lean_service and not devices_arg:
            # Service-type/id focus: still list devices so drill mcp_call
            # (exec_show / health) is gated against real names. Lean collect
            # skips HW/physical; inventory is for allowlisting only.
            focus = []
            spine_devices = list(all_names)
            filter_services_to_devices = False
            print(
                f"[focus] lean service type={service_type!r} id={service_id!r} "
                f"devices={len(all_names)}",
                file=sys.stderr,
            )
        else:
            focus = (
                filter_device_names(all_names, devices_arg) if devices_arg else []
            )
            if devices_arg and not focus:
                print(
                    f"No devices matched --devices {devices_arg!r}",
                    file=sys.stderr,
                )
                return 2

            if focus:
                if run_isis or run_bgp:
                    spine_devices = await expand_spine_devices(
                        client,
                        focus,
                        all_names,
                        expand_isis=run_isis,
                        expand_bgp=run_bgp,
                    )
                    print(
                        f"[focus] seeds={focus} spine_devices={spine_devices}",
                        file=sys.stderr,
                    )
                else:
                    spine_devices = list(focus)
                    if run_device:
                        print(
                            f"[focus] lean device devices={spine_devices}",
                            file=sys.stderr,
                        )
                filter_services_to_devices = bool(run_service)
            else:
                spine_devices = all_names
                filter_services_to_devices = False

        case.focus_devices = list(focus)

        set_mcp_stage("spines")
        await run_mandatory_spines(
            client,
            settings,
            case,
            spine_devices,
            run_isis=run_isis,
            run_bgp=run_bgp,
            run_service=run_service,
            run_device=run_device,
            service_type=service_type,
            service_id=service_id,
            filter_services_to_devices=filter_services_to_devices,
            spine_concurrent_devices=getattr(args, "spine_concurrent_devices", 1),
        )
        if nso_unreachable_reason():
            return NSO_UNREACHABLE_EXIT
        if lean_service:
            matched = services_from_case(case)
            if not matched:
                bits = []
                if service_type:
                    bits.append(f"type={service_type!r}")
                if service_id:
                    bits.append(f"id={service_id!r}")
                print(
                    "No services matched "
                    + " and ".join(bits)
                    + " — not falling back to a broad scan.",
                    file=sys.stderr,
                )
                return 2
            endpoints = endpoint_devices_from_services(matched)
            case.focus_devices = list(endpoints)
            case.device_names = list(endpoints) or list(case.device_names or [])
            spine_devices = list(endpoints) or spine_devices
            print(
                f"[focus] matched_services={len(matched)} "
                f"endpoint_devices={endpoints}",
                file=sys.stderr,
            )
            from diagnostic_mas.dataplane_verify import init_service_coverage

            init_service_coverage(case)

        if focus:
            case.device_names = list(focus)
            if run_service and (run_isis or run_bgp):
                _refilter_service_evidence(
                    case, focus, service_type, service_id
                )

        if not skip_llm and run_service:
            from diagnostic_mas.dataplane_verify import (
                init_service_coverage,
                run_dataplane_verify_phase,
            )

            if not lean_service and not case.service_coverage:
                init_service_coverage(case)

            set_mcp_stage("dataplane")
            await run_dataplane_verify_phase(
                client,
                settings,
                case,
                device_names=set(spine_devices or all_names or case.device_names),
                skip_llm=skip_llm,
                suspicious_only=lean_service,
                explicit_service=bool(service_id),
                max_services=getattr(args, "max_dataplane_services", None),
                max_per_category=getattr(
                    args, "max_dataplane_per_category", None
                ),
                concurrent_works=getattr(args, "dataplane_concurrent_works", 1),
                category_rotate_seed=run_id,
            )

        # Lean service focus: skip multi-minute autonomous planner; still allow
        # budgeted drill, then optional underlay expand when evidence needs it.
        if lean_service:
            print(
                "[focus] lean service: skip autonomous "
                "(dataplane + optional drill/expand)",
                file=sys.stderr,
            )
        elif (
            not skip_llm
            and not getattr(case, "llm_halt_reason", None)
            and (
                case.budget.max_deep_checks > 0 or case.budget.max_handoffs > 0
            )
        ):
            set_mcp_stage("autonomous")
            await run_autonomous_loop(
                client,
                settings,
                case,
                device_names=spine_devices or all_names,
            )
        elif getattr(case, "llm_halt_reason", None):
            print(
                "[llm] provider budget exceeded — skipping autonomous/drill/"
                f"summary LLM ({case.llm_halt_reason})",
                file=sys.stderr,
            )

        if (
            not skip_llm
            and not getattr(case, "llm_halt_reason", None)
            and case.budget.max_drill_issues > 0
            and case.budget.max_tools_per_drill > 0
            and (not lean_service or any(
                i.get("status") == "open" for i in case.issues
            ))
        ):
            from diagnostic_mas.drill import run_drill_phase

            set_mcp_stage("drill")
            await run_drill_phase(
                client,
                settings,
                case,
                device_names=set(spine_devices or all_names or case.device_names),
                skip_llm=skip_llm,
            )

        if lean_service and not skip_llm:
            from diagnostic_mas.service_expand import (
                maybe_expand_underlay_for_service_focus,
            )

            set_mcp_stage("expand")
            await maybe_expand_underlay_for_service_focus(
                client, case, physical_edges=[]
            )

        from nso_facts.mcp_client import successful_live_devices

        case.live_verified_devices = successful_live_devices()
        set_mcp_stage(None)

    # Collection that lost NSO is not evidence about the network: report nothing.
    if nso_unreachable_reason():
        return NSO_UNREACHABLE_EXIT

    from diagnostic_mas.ingest import ingest_quarantined_devices

    ingest_quarantined_devices(case)

    # LLM writes only the top Summary; device/service body is deterministic.
    # Compare to last published case when available (even on dry-run).
    from diagnostic_mas.case_delta import (
        compact_delta_for_llm,
        compute_case_delta,
        load_previous_case,
        service_fault_history,
    )

    state_dir = diagnostic_mas_state_dir(settings)
    previous_case, previous_run_id = load_previous_case(state_dir)
    case.last_known_service_faults = service_fault_history(
        previous_case or {}, case_to_dict(case),
        previous_run_id=previous_run_id, run_id=run_id,
    )
    case_delta = None
    if previous_case is not None:
        case_delta = compute_case_delta(previous_case, case_to_dict(case))
        case_delta["last_known_service_faults"] = case.last_known_service_faults

    # Keep the full fault ledger for persistence; scope only its report view.
    report_case = case
    if service_type or service_id:
        from diagnostic_mas.dataplane_verify import iter_service_records

        names = {str(rec.get("name") or "") for _, rec in iter_service_records(case)}
        report_case = copy(case)
        report_case.last_known_service_faults = [
            row for row in case.last_known_service_faults if row.get("service") in names
        ]
        if case_delta is not None:
            case_delta = dict(case_delta)
            case_delta["last_known_service_faults"] = report_case.last_known_service_faults

    narrative = ""
    reporting_notes: list[str] = []
    if getattr(case, "llm_halt_reason", None):
        reporting_notes.append(
            f"Provider LLM halted: {case.llm_halt_reason}. "
            "Completed digs preserved; further LLM requests skipped."
        )
    if not skip_llm:
        try:
            narrative = await summary_narrative(
                report_case,
                settings,
                skip_llm=False,
                deterministic_summary=bool(
                    getattr(args, "deterministic_summary", False)
                )
                or bool(getattr(case, "llm_halt_reason", None)),
                case_delta=compact_delta_for_llm(case_delta),
                previous_run_id=previous_run_id,
            )
        except Exception as exc:  # noqa: BLE001
            reporting_notes.append(f"Final LLM summary failed: {exc}")
            narrative = ""
    duration = time.monotonic() - t0
    report = render_report(
        report_case,
        summary=narrative or None,
        services_detail=bool(
            getattr(args, "services_detail", False) or service_type or service_id
        ),
        duration_seconds=duration,
        dry_run=dry_run,
        reporting_notes=reporting_notes or None,
        llm_model=None if skip_llm else str(settings.fabric_model or "").strip() or None,
        case_delta=case_delta,
        previous_run_id=previous_run_id,
    )
    print(report)

    # A channel that cannot deliver must not stop the others, the saved report or metrics.
    delivery_failures: list[tuple[str, str]] = []
    if not dry_run:
        out = persist_case(
            state_dir,
            run_id=run_id,
            case=case,
            report=report,
        )
        from diagnostic_mas.html_report import notification_digest
        from urllib.parse import quote

        report_url = None
        if settings.diagnostic_report_base_url:
            report_url = (settings.diagnostic_report_base_url.rstrip("/") + "/"
                          + quote(out.parent.name, safe="") + "/report.html")
        channels = publish_all(
            notification_digest(report, run_id),
            run_id,
            settings,
            attachment_path=out.parent / "report.html",
            report_url=report_url,
            failures=delivery_failures,
        )
        print(f"\nWrote case/report under {out.parent}", file=sys.stderr)
        if channels:
            print(f"Published to: {', '.join(channels)}", file=sys.stderr)
        elif not delivery_failures:
            print(
                "No Slack/email configured (artifacts saved only)",
                file=sys.stderr,
            )
        for channel, reason in delivery_failures:
            print(f"Delivery failed: {channel} — {reason}", file=sys.stderr)
        if not bool(getattr(args, "skip_metrics", False)):
            duration = time.monotonic() - t0
            snapshot = metrics_snapshot_from_case(case, run_id=run_id, report_url=report_url)
            if push_phase1_metrics(
                snapshot,
                settings,
                success=True,
                duration_seconds=duration,
                pipeline="diagnostic",
            ):
                print(
                    f"Pushed metrics to Pushgateway "
                    f"(job={settings.prometheus_job}, "
                    f"instance={settings.prometheus_instance})",
                    file=sys.stderr,
                )
    else:
        print(
            "[DRY RUN] not publishing (no Slack/email) and not saving "
            "case/report state — use --publish or set DRY_RUN=0 to deliver"
            + ("; MCP results saved separately (--save-mcp-results)"
               if getattr(args, "save_mcp_results", False) else ""),
            file=sys.stderr,
        )
    return 1 if delivery_failures else 0


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
        "--spine-concurrent-devices", type=_positive_worker_count, default=1,
        help="Maximum simultaneous device calls in service operational checks (default 1)",
    )
    parser.add_argument(
        "--dataplane-concurrent_works", type=_positive_worker_count, default=1,
        help="Concurrent service dataplane digs (default 1); overlapping endpoints wait",
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
        default=None,
        help=(
            "Cap how many services get dataplane LLM verify in total. "
            "Default without --max-dataplane-per-category: one best instance "
            "per typed prompt category (dataplane_agent_<type>.txt on disk); "
            "two if only one typed category is present. Soft-error live L2 is "
            "preferred within the cap, not added beyond it."
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
        help="Force no Slack/email and no state write (default when DRY_RUN=1)",
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # --max-drills is a deprecated alias for --max-tools-per-drill
    if getattr(args, "max_drills", None) is not None:
        args.max_tools_per_drill = int(args.max_drills)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
