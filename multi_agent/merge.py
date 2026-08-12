"""Merge per-agent results into one report payload."""

from __future__ import annotations

from typing import Any

from multi_agent.base import AgentResult, format_domain_section
from multi_agent.device_agent import format_device_section
from multi_agent.executive import format_executive_header
from multi_agent.fleet_details import format_inventory_hardware_section


def merge_results(results: list[AgentResult]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    by_agent: dict[str, Any] = {}
    for result in results:
        for issue in result.issues:
            row = dict(issue)
            row.setdefault("agent", result.name)
            row.setdefault("layer", result.layer)
            issues.append(row)
        by_agent[result.name] = result.fact_pack()
    return {
        "agents": by_agent,
        "issues": issues,
        "issues_total": len(issues),
    }


def build_merged_report(
    results: list[AgentResult],
    *,
    summary_text: str | None = None,
    run_id: str | None = None,
    fleet_pack: dict[str, Any] | None = None,
    fabric_api_url: str | None = None,
    fabric_model: str | None = None,
    mcp_server_cmd: str | None = None,
    llm_skipped: bool = False,
) -> str:
    parts: list[str] = [
        format_executive_header(
            results,
            run_id=run_id,
            summary_text=summary_text,
            fleet_pack=fleet_pack,
            fabric_api_url=fabric_api_url,
            fabric_model=fabric_model,
            mcp_server_cmd=mcp_server_cmd,
            llm_skipped=llm_skipped,
        ),
        "-----------------------------------------------------",
        "Detailed Analysis",
        "-----------------------------------------------------",
        "",
        "Bidirectional checks: each adjacency/session requires both sides.",
        "Device sections use topology seed + hardware/CPU spine (+ optional MCP plan).",
        "",
    ]
    domain = [r for r in results if r.layer in ("underlay", "routing")]
    devices = [r for r in results if r.layer == "device"]
    for result in domain:
        parts.append(format_domain_section(result))
    inv_hw = format_inventory_hardware_section(fleet_pack)
    if inv_hw:
        parts.append(inv_hw)
        parts.append("")
    if devices:
        parts.append("## Devices")
        parts.append("")
        for result in devices:
            parts.append(format_device_section(result))
    parts.append("")
    return "\n".join(parts)
