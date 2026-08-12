"""CLI entrypoint: collect → delta → summarize → publish."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

from agent.collect import collect_snapshot
from agent.config import (
    DEFAULT_MAX_SERVICE_TYPES,
    email_configured,
    load_settings,
    resolve_dry_run,
    validate_email_settings,
)
from agent.delta import compute_delta
from agent.fabric_key import prepare_fabric_llm
from agent.metrics import push_phase1_metrics
from agent.publish import publish_all, publish_stdout, save_run_artifacts, send_test_email
from agent.report_format import ReportOutputs
from agent.summarize import summarize_report


def _load_previous(state_dir: Path) -> dict | None:
    latest = state_dir / "latest.json"
    if not latest.is_file():
        return None
    return json.loads(latest.read_text(encoding="utf-8"))


async def _probe_routing_async(*, device: str | None) -> int:
    from agent.mcp_client import mcp_session
    from agent.topology.probe import format_probe_report, probe_routing_layer

    settings = load_settings()
    async with mcp_session(settings) as client:
        report = await probe_routing_layer(client, device=device)
    print(format_probe_report(report))
    return 0


async def _probe_underlay_async(*, device: str | None) -> int:
    from agent.mcp_client import mcp_session
    from agent.topology.probe import format_probe_report, probe_underlay_layer

    settings = load_settings()
    async with mcp_session(settings) as client:
        report = await probe_underlay_layer(client, device=device)
    print(format_probe_report(report))
    return 0


async def _probe_physical_async(*, device: str | None) -> int:
    from agent.mcp_client import mcp_session
    from agent.topology.probe import format_probe_report, probe_physical_layer

    settings = load_settings()
    async with mcp_session(settings) as client:
        report = await probe_physical_layer(client, device=device)
    print(format_probe_report(report))
    return 0


async def run_async(
    *,
    dry_run: bool = True,
    skip_llm: bool = False,
    list_tools: bool = False,
    test_email: bool = False,
    max_service_types: int | None = None,
) -> int:
    settings = load_settings()
    if max_service_types is not None:
        settings = replace(settings, max_service_types=max(1, max_service_types))
    _, force_skip_llm = prepare_fabric_llm(settings)
    if force_skip_llm:
        skip_llm = True

    if test_email:
        if not email_configured(settings):
            raise RuntimeError("EMAIL_TO is not set — configure email in .env first")
        warnings = validate_email_settings(settings)
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        send_test_email(settings)
        print(
            f"Email test sent to: {', '.join(settings.email_to)}",
            file=sys.stderr,
        )
        return 0

    if email_configured(settings):
        warnings = validate_email_settings(settings)
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)

    if list_tools:
        from agent.mcp_client import mcp_session

        async with mcp_session(settings) as client:
            tools = await client.list_tools()
            for t in tools:
                print(f"- {t.name}: {t.description}")
        return 0

    previous = _load_previous(settings.state_dir)
    t0 = time.monotonic()
    snapshot = await collect_snapshot(settings, skip_llm=skip_llm)
    delta = compute_delta(snapshot, previous)

    if skip_llm:
        report_out: ReportOutputs | str = json.dumps(
            {"snapshot": snapshot, "delta": delta}, indent=2, default=str
        )
    else:
        report_out = summarize_report(snapshot, delta, settings)

    if isinstance(report_out, str):
        publish_stdout(report_out)
        report_for_save = report_out
    else:
        publish_stdout(report_out.markdown)
        report_for_save = report_out.markdown

    duration = time.monotonic() - t0

    if dry_run:
        print(
            "\n[DRY RUN] not publishing (no Slack/email) and not saving state — "
            "use --publish or set DRY_RUN=0 to deliver",
            file=sys.stderr,
        )
        return 0

    save_run_artifacts(snapshot, delta, report_for_save, settings)
    channels = publish_all(report_out, snapshot.get("run_id", "unknown"), settings)
    if channels:
        print(f"\nDelivered via: {', '.join(channels)}", file=sys.stderr)
    else:
        print(
            "\nNo delivery channels configured (set SLACK_WEBHOOK_URL and/or EMAIL_TO)",
            file=sys.stderr,
        )

    if push_phase1_metrics(
        snapshot,
        settings,
        success=True,
        duration_seconds=duration,
        pipeline="agent",
        delta=delta,
    ):
        print(
            f"Pushed metrics to Pushgateway "
            f"(job={settings.prometheus_job}, instance={settings.prometheus_instance})",
            file=sys.stderr,
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="NSO summary agent")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print report only; do not update latest.json or send notifications",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Send Slack/email and update state (overrides DRY_RUN=1)",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Skip FABRIC AI; dump JSON snapshot + delta",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="List NSO MCP tools and exit",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Validate SMTP settings and send a test message to EMAIL_TO",
    )
    parser.add_argument(
        "--max-service-types",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Max service types to query via get_services per run "
            f"(default: MAX_SERVICE_TYPES env or {DEFAULT_MAX_SERVICE_TYPES})"
        ),
    )
    parser.add_argument(
        "--probe-physical",
        metavar="DEVICE",
        nargs="?",
        const="__all__",
        help="Probe static interface discovery (optional DEVICE; default all). Writes JSON to stdout.",
    )
    parser.add_argument(
        "--probe-routing",
        metavar="DEVICE",
        nargs="?",
        const="__all__",
        help="Probe BGP routing discovery (optional DEVICE; default all). Writes JSON to stdout.",
    )
    parser.add_argument(
        "--probe-underlay",
        metavar="DEVICE",
        nargs="?",
        const="__all__",
        help="Probe IS-IS underlay discovery (optional DEVICE; default all). Writes JSON to stdout.",
    )
    args = parser.parse_args()

    try:
        if args.probe_underlay is not None:
            code = asyncio.run(
                _probe_underlay_async(
                    device=None if args.probe_underlay == "__all__" else args.probe_underlay
                )
            )
            sys.exit(code)
        if args.probe_routing is not None:
            code = asyncio.run(
                _probe_routing_async(
                    device=None if args.probe_routing == "__all__" else args.probe_routing
                )
            )
            sys.exit(code)
        if args.probe_physical is not None:
            code = asyncio.run(
                _probe_physical_async(
                    device=None if args.probe_physical == "__all__" else args.probe_physical
                )
            )
            sys.exit(code)
        settings = load_settings()
        effective_dry_run = resolve_dry_run(
            settings=settings,
            publish=args.publish,
            dry_run_flag=args.dry_run,
        )
        code = asyncio.run(
            run_async(
                dry_run=effective_dry_run,
                skip_llm=args.skip_llm,
                list_tools=args.list_tools,
                test_email=args.test_email,
                max_service_types=args.max_service_types,
            )
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(code)


if __name__ == "__main__":
    main()
