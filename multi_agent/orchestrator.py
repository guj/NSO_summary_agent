"""Orchestrate ISIS + BGP + device agents + fleet spine, merge, summarize."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.config import Settings
from nso_facts.delta import compute_delta
from nso_facts.mcp_client import call_mcp, mcp_session
from nso_facts.metrics import push_phase1_metrics
from agent.publish import publish_all, publish_stdout
from nso_facts.topology.devices import parse_device_names
from nso_facts.topology.physical import collect_static_physical
from multi_agent.base import AgentResult
from multi_agent.bgp_agent import run_bgp_agent
from multi_agent.device_agent import run_device_agents
from multi_agent.fleet_spine import (
    assemble_topology,
    collect_fleet_spine,
    merge_device_feedback,
)
from multi_agent.isis_agent import run_isis_agent
from multi_agent.merge import build_merged_report, merge_results
from multi_agent.focus import resolve_layer_focus
from multi_agent.state_paths import (
    load_previous_latest,
    multi_agent_state_dir,
    persist_multi_agent_run,
)

_PROMPTS = Path(__file__).resolve().parent / "prompts"


def metrics_snapshot_from_run(
    *,
    fleet_pack: dict[str, Any] | None,
    device_names: list[str],
    physical_edges: list[dict[str, Any]],
    isis: AgentResult,
    bgp: AgentResult,
) -> dict[str, Any]:
    """Build a Phase-1-compatible snapshot for Pushgateway."""
    if fleet_pack is not None:
        return {
            "topology": fleet_pack.get("topology"),
            "fleet_sync": fleet_pack.get("fleet_sync"),
            "counts": fleet_pack.get("counts") or {},
            "system_health": fleet_pack.get("system_health") or {},
            "hardware_health": fleet_pack.get("hardware_health") or {},
            "delta": fleet_pack.get("delta"),
        }
    topology = assemble_topology(
        device_names=device_names,
        physical_edges=physical_edges,
        physical_op_edges=[],
        isis=isis,
        bgp=bgp,
    )
    return {
        "topology": topology,
        "fleet_sync": None,
        "counts": {},
        "system_health": {},
        "hardware_health": {},
        "delta": None,
    }


async def run_orchestrator(
    settings: Settings,
    *,
    spine_only: bool = False,
    skip_llm: bool = False,
    dry_run: bool = True,
    device_names_filter: list[str] | None = None,
    all_devices: bool = False,
    skip_devices: bool = False,
    max_devices: int = 8,
    skip_fleet_spine: bool = False,
    skip_metrics: bool = False,
    isis_only: bool = False,
    bgp_only: bool = False,
) -> dict[str, Any]:
    run_isis, run_bgp = resolve_layer_focus(
        isis_only=isis_only, bgp_only=bgp_only
    )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fleet_raw: dict[str, Any] | None = None
    fleet_pack: dict[str, Any] | None = None
    t0 = time.monotonic()
    device_names: list[str] = []
    physical_edges: list[dict[str, Any]] = []

    async with mcp_session(settings) as client:
        list_result = await call_mcp(client, "list_devices")
        device_names = parse_device_names(list_result)
        physical_edges, _, _ = await collect_static_physical(client, device_names)

        isis = AgentResult(name="isis", layer="underlay")
        bgp = AgentResult(name="bgp", layer="routing")
        if run_isis:
            isis = await run_isis_agent(
                client,
                settings,
                device_names,
                spine_only=spine_only,
                skip_llm=skip_llm,
                physical_edges=physical_edges,
            )
        if run_bgp:
            bgp = await run_bgp_agent(
                client,
                settings,
                device_names,
                spine_only=spine_only,
                skip_llm=skip_llm,
            )

        device_results: list[Any] = []
        if not skip_devices:
            device_results = await run_device_agents(
                client,
                settings,
                device_names,
                isis,
                bgp,
                devices=device_names_filter,
                all_devices=all_devices,
                max_devices=max_devices,
                spine_only=spine_only,
                skip_llm=skip_llm,
            )

        if not skip_fleet_spine:
            fleet_raw = await collect_fleet_spine(
                client,
                settings,
                device_names,
                physical_edges=physical_edges,
            )
            fleet_raw = merge_device_feedback(fleet_raw, device_results)
            topology = assemble_topology(
                device_names=device_names,
                physical_edges=physical_edges,
                physical_op_edges=list(
                    fleet_raw.get("physical_operational_edges") or []
                ),
                isis=isis,
                bgp=bgp,
                extra_issues=list(fleet_raw.get("physical_issues") or []),
            )
            current_for_delta = {
                "counts": fleet_raw.get("counts") or {},
                "services": fleet_raw.get("services") or {},
            }
            ma_state = multi_agent_state_dir(settings)
            delta = compute_delta(
                current_for_delta, load_previous_latest(ma_state)
            )
            fleet_pack = {
                "counts": fleet_raw.get("counts") or {},
                "services": fleet_raw.get("services") or {},
                "fleet_sync": fleet_raw.get("fleet_sync"),
                "system_health": fleet_raw.get("system_health") or {},
                "hardware_health": fleet_raw.get("hardware_health") or {},
                "topology": topology,
                "delta": delta,
                "physical_issues": list(fleet_raw.get("physical_issues") or []),
            }

    results: list[AgentResult] = []
    if run_isis:
        results.append(isis)
    if run_bgp:
        results.append(bgp)
    results.extend(device_results)
    merged = merge_results(results)
    if fleet_pack is not None:
        merged["fleet"] = {
            "counts": fleet_pack.get("counts"),
            "delta": fleet_pack.get("delta"),
            "devices_with_hw": sorted((fleet_pack.get("hardware_health") or {}).keys()),
            "devices_with_sys": sorted((fleet_pack.get("system_health") or {}).keys()),
        }
    summary = None
    if not spine_only and not skip_llm and settings.fabric_api_key:
        summary = _summarize(settings, merged)

    report = build_merged_report(
        results,
        summary_text=summary,
        run_id=run_id,
        fleet_pack=fleet_pack,
        fabric_api_url=settings.fabric_api_url,
        fabric_model=settings.fabric_model,
        mcp_server_cmd=settings.mcp_server_cmd,
        llm_skipped=spine_only or skip_llm or not settings.fabric_api_key,
    )

    artifact_files: dict[str, str] = {
        "merged.json": json.dumps(merged, indent=2, default=str),
    }
    if run_isis:
        artifact_files["isis.json"] = json.dumps(
            isis.fact_pack(), indent=2, default=str
        )
    if run_bgp:
        artifact_files["bgp.json"] = json.dumps(
            bgp.fact_pack(), indent=2, default=str
        )
    for dr in device_results:
        safe = str(dr.name).replace(":", "_")
        artifact_files[f"{safe}.json"] = json.dumps(
            dr.fact_pack(), indent=2, default=str
        )
    if fleet_raw is not None:
        artifact_files["fleet.json"] = json.dumps(
            {
                "counts": fleet_raw.get("counts"),
                "services": fleet_raw.get("services"),
                "fleet_sync": fleet_raw.get("fleet_sync"),
                "system_health": fleet_raw.get("system_health"),
                "hardware_health": fleet_raw.get("hardware_health"),
                "physical_issues": fleet_raw.get("physical_issues"),
                "delta": (fleet_pack or {}).get("delta"),
            },
            indent=2,
            default=str,
        )

    publish_stdout(report)
    out_dir: str | None = None
    if not dry_run:
        ma_state = multi_agent_state_dir(settings)
        counts = (fleet_pack or {}).get("counts") or {}
        services = (fleet_pack or {}).get("services") or {}
        delta_obj = (fleet_pack or {}).get("delta")
        run_path = persist_multi_agent_run(
            ma_state,
            run_id=run_id,
            counts=counts if isinstance(counts, dict) else {},
            services=services if isinstance(services, dict) else {},
            delta=delta_obj if isinstance(delta_obj, dict) else None,
            report=report,
            artifact_files=artifact_files,
        )
        out_dir = str(run_path)
        publish_all(
            report,
            run_id,
            settings,
        )

    metrics_pushed = False
    if not skip_metrics:
        snapshot = metrics_snapshot_from_run(
            fleet_pack=fleet_pack,
            device_names=device_names,
            physical_edges=physical_edges,
            isis=isis,
            bgp=bgp,
        )
        duration = time.monotonic() - t0
        if push_phase1_metrics(
            snapshot,
            settings,
            success=True,
            duration_seconds=duration,
            pipeline="multi-agent",
            delta=snapshot.get("delta")
            if isinstance(snapshot.get("delta"), dict)
            else None,
        ):
            metrics_pushed = True
            print(
                f"Pushed metrics to Pushgateway "
                f"(job={settings.prometheus_job}, "
                f"instance={settings.prometheus_instance})",
                file=sys.stderr,
            )

    return {
        "run_id": run_id,
        "out_dir": out_dir,
        "merged": merged,
        "report": report,
        "fleet_pack": fleet_pack,
        "metrics_pushed": metrics_pushed,
    }


def _summarize(settings: Settings, merged: dict[str, Any]) -> str:
    from agent.summarize import fabric_openai_client

    prompt_path = _PROMPTS / "summary.txt"
    system = (
        prompt_path.read_text(encoding="utf-8")
        if prompt_path.is_file()
        else (
            "Summarize IS-IS/BGP issues and suggest read-only remedy hypotheses. "
            "Do not invent data."
        )
    )
    client = fabric_openai_client(settings)
    user = json.dumps(summary_user_payload(merged), default=str)
    resp = client.chat.completions.create(
        model=settings.fabric_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip() if resp.choices else ""


def summary_user_payload(merged: dict[str, Any]) -> dict[str, Any]:
    """Compact fact pack for the multi-agent FABRIC summary + remedies call."""
    agents_out: dict[str, Any] = {}
    for name, pack in (merged.get("agents") or {}).items():
        if not isinstance(pack, dict):
            continue
        agents_out[name] = {
            "layer": pack.get("layer"),
            "operational_summary": pack.get("operational_summary"),
            "issue_count": len(pack.get("issues") or []),
            "evidence": _compact_evidence(pack.get("evidence") or []),
        }
    return {
        "issues_total": merged.get("issues_total"),
        "issues": (merged.get("issues") or [])[:80],
        "fleet": merged.get("fleet"),
        "agents": agents_out,
    }


def _compact_evidence(evidence: list[Any], *, limit: int = 12) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in evidence[:limit]:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {
            "check": item.get("check"),
            "reason": item.get("reason"),
            "issue_id": item.get("issue_id"),
        }
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        device = args.get("device_name") or args.get("device")
        if device:
            row["device"] = device
        cmd = args.get("input_command") or args.get("command")
        if cmd:
            row["command"] = cmd
        if item.get("error"):
            row["error"] = str(item.get("error"))[:240]
        elif item.get("result") is not None:
            # Keep a short string preview, not full MCP dumps
            preview = item.get("result")
            text = preview if isinstance(preview, str) else json.dumps(preview, default=str)
            row["result_preview"] = text[:400]
        out.append(row)
    return out
