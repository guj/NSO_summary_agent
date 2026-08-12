"""CLI: multi-agent IS-IS + BGP + optional device workers."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from agent.config import load_settings, resolve_dry_run  # noqa: E402
from agent.fabric_key import prepare_fabric_llm  # noqa: E402
from multi_agent.orchestrator import run_orchestrator  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Multi-agent IS-IS + BGP + device checks"
    )
    parser.add_argument(
        "--spine-only",
        action="store_true",
        help="MCP spines only; no LLM plan/summary",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Skip Fabric plan/summary",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Send Slack/email (overrides DRY_RUN=1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force no Slack/email (default when DRY_RUN=1)",
    )
    parser.add_argument(
        "--devices",
        type=str,
        default="",
        help="Comma-separated device names for device agents (default: devices on issues)",
    )
    parser.add_argument(
        "--all-devices",
        action="store_true",
        help="Run device agent for every inventory device (capped by --max-devices)",
    )
    parser.add_argument(
        "--skip-devices",
        action="store_true",
        help="Only ISIS + BGP agents",
    )
    parser.add_argument(
        "--max-devices",
        type=int,
        default=8,
        help="Cap on device agents (default 8)",
    )
    parser.add_argument(
        "--skip-fleet-spine",
        action="store_true",
        help="Skip services/sync/CPU/hardware/inventory executive collect",
    )
    parser.add_argument(
        "--skip-metrics",
        action="store_true",
        help="Do not push Phase 1 gauges to Prometheus Pushgateway",
    )
    layer = parser.add_mutually_exclusive_group()
    layer.add_argument(
        "--isis-only",
        action="store_true",
        help="Run IsisAgent only (skip BGP spine)",
    )
    layer.add_argument(
        "--bgp-only",
        action="store_true",
        help="Run BgpAgent only (skip IS-IS spine)",
    )
    args = parser.parse_args(argv)
    device_filter = [p.strip() for p in args.devices.split(",") if p.strip()] or None
    settings = load_settings()
    _, force_skip_llm = prepare_fabric_llm(settings)
    skip_llm = args.skip_llm or args.spine_only or force_skip_llm
    dry_run = resolve_dry_run(
        settings=settings,
        publish=args.publish,
        dry_run_flag=args.dry_run,
    )
    result = asyncio.run(
        run_orchestrator(
            settings,
            spine_only=args.spine_only,
            skip_llm=skip_llm,
            dry_run=dry_run,
            device_names_filter=device_filter,
            all_devices=args.all_devices,
            skip_devices=args.skip_devices,
            max_devices=max(0, args.max_devices),
            skip_fleet_spine=args.skip_fleet_spine,
            skip_metrics=args.skip_metrics,
            isis_only=args.isis_only,
            bgp_only=args.bgp_only,
        )
    )
    if result.get("out_dir"):
        print(f"\nWrote artifacts under {result['out_dir']}", file=sys.stderr)
    if dry_run:
        print(
            "[DRY RUN] not publishing (no Slack/email) and not saving "
            "state/multi_agent/ — use --publish or set DRY_RUN=0 to deliver",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
