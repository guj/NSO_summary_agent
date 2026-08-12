"""IS-IS agent: bidirectional underlay spine + optional LLM plan."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.config import Settings
from nso_facts.topology.graph import (
    summarize_operational_underlay,
    summarize_static_underlay,
)
from nso_facts.topology.physical import collect_static_physical
from nso_facts.topology.underlay import (
    collect_operational_underlay,
    collect_static_underlay,
)
from multi_agent.base import (
    ISIS_ALLOWLIST,
    AgentResult,
    dedupe_issues,
    gate_plan,
    parse_plan_json,
)
from multi_agent.deep_checks import run_deep_checks

_PROMPTS = Path(__file__).resolve().parent / "prompts"


async def run_isis_agent(
    client: Any,
    settings: Settings,
    device_names: list[str],
    *,
    spine_only: bool = False,
    skip_llm: bool = False,
    physical_edges: list[dict[str, Any]] | None = None,
) -> AgentResult:
    if physical_edges is None:
        physical_edges, _, _ = await collect_static_physical(client, device_names)

    static_edges, static_issues, static_cov = await collect_static_underlay(
        client, device_names, physical_edges=physical_edges
    )
    op_edges, op_issues, op_cov = await collect_operational_underlay(
        client,
        device_names,
        static_edges,
        physical_edges=physical_edges,
    )

    issues = dedupe_issues(static_issues + op_issues)
    result = AgentResult(
        name="isis",
        layer="underlay",
        static_summary=summarize_static_underlay(static_edges),
        operational_summary=summarize_operational_underlay(op_edges),
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
        allowlist=ISIS_ALLOWLIST,
        device_names=set(device_names),
    )
    result.plan = gated
    if gated:
        result.evidence = await run_deep_checks(client, gated)
    return result


async def _llm_plan(settings: Settings, result: AgentResult) -> list[dict[str, Any]]:
    from agent.summarize import fabric_openai_client

    prompt_path = _PROMPTS / "isis_plan.txt"
    system = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else (
        "Propose JSON array of exec_show tasks for IS-IS issues only."
    )
    user = json.dumps(
        {
            "operational_summary": result.operational_summary,
            "issues": result.issues[:40],
            "allowlist": sorted(ISIS_ALLOWLIST),
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
