"""BGP agent: bidirectional routing spine + optional LLM plan."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.config import Settings
from nso_facts.topology.graph import (
    summarize_operational_routing,
    summarize_static_routing,
)
from nso_facts.topology.routing import (
    collect_operational_routing,
    collect_static_routing,
)
from multi_agent.base import (
    BGP_ALLOWLIST,
    AgentResult,
    dedupe_issues,
    gate_plan,
    parse_plan_json,
)
from multi_agent.deep_checks import run_deep_checks

_PROMPTS = Path(__file__).resolve().parent / "prompts"


async def run_bgp_agent(
    client: Any,
    settings: Settings,
    device_names: list[str],
    *,
    spine_only: bool = False,
    skip_llm: bool = False,
) -> AgentResult:
    static_edges, static_issues, static_cov = await collect_static_routing(
        client, device_names
    )
    op_edges, op_issues, op_cov = await collect_operational_routing(
        client, device_names, static_edges
    )

    issues = dedupe_issues(static_issues + op_issues)
    result = AgentResult(
        name="bgp",
        layer="routing",
        static_summary=summarize_static_routing(static_edges),
        operational_summary=summarize_operational_routing(op_edges),
        issues=issues,
        static_edges=static_edges,
        operational_edges=op_edges,
        coverage={
            "devices_total": max(
                static_cov.get("devices_total", 0), op_cov.get("devices_total", 0)
            ),
            "devices_queried": max(
                static_cov.get("devices_queried", 0),
                op_cov.get("devices_queried", 0),
            ),
            "devices_failed": max(
                static_cov.get("devices_failed", 0),
                op_cov.get("devices_failed", 0),
            ),
        },
    )

    if spine_only or skip_llm or not settings.fabric_api_key:
        return result

    plan_raw = await _llm_plan(settings, result)
    gated = gate_plan(
        plan_raw,
        allowlist=BGP_ALLOWLIST,
        device_names=set(device_names),
    )
    result.plan = gated
    if gated:
        result.evidence = await run_deep_checks(client, gated)
    return result


async def _llm_plan(settings: Settings, result: AgentResult) -> list[dict[str, Any]]:
    from agent.summarize import fabric_openai_client

    prompt_path = _PROMPTS / "bgp_plan.txt"
    system = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else (
        "Propose JSON array of MCP tasks for BGP issues only."
    )
    user = json.dumps(
        {
            "operational_summary": result.operational_summary,
            "issues": result.issues[:40],
            "allowlist": sorted(BGP_ALLOWLIST),
        },
        default=str,
    )
    client = fabric_openai_client(settings)
    resp = client.chat.completions.create(
        model=settings.fabric_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
    )
    text = (resp.choices[0].message.content or "") if resp.choices else ""
    return parse_plan_json(text)
