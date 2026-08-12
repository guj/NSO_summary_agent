"""Per-device agent: topology seed + hardware spine + optional LLM MCP plan."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.config import Settings
from nso_facts.hardware_health import collect_hardware_health
from nso_facts.system_health import collect_system_health
from multi_agent.base import (
    DEVICE_ALLOWLIST,
    AgentResult,
    format_evidence_preview,
    gate_plan,
    parse_plan_json,
)
from multi_agent.deep_checks import run_deep_checks
from multi_agent.topology_slice import (
    devices_with_topology_issues,
    slice_topology_for_device,
)

_PROMPTS = Path(__file__).resolve().parent / "prompts"
_MAX_DEVICES_DEFAULT = 8


async def run_device_agents(
    client: Any,
    settings: Settings,
    inventory: list[str],
    isis: AgentResult,
    bgp: AgentResult,
    *,
    devices: list[str] | None = None,
    all_devices: bool = False,
    max_devices: int = _MAX_DEVICES_DEFAULT,
    spine_only: bool = False,
    skip_llm: bool = False,
) -> list[AgentResult]:
    """Run device workers for selected boxes."""
    if devices:
        selected = [d for d in devices if d in set(inventory)]
    elif all_devices:
        selected = list(inventory)
    else:
        selected = devices_with_topology_issues(isis, bgp, inventory)
    selected = selected[: max(0, max_devices)]

    results: list[AgentResult] = []
    for device in selected:
        results.append(
            await run_device_agent(
                client,
                settings,
                device,
                isis,
                bgp,
                spine_only=spine_only,
                skip_llm=skip_llm,
            )
        )
    return results


async def run_device_agent(
    client: Any,
    settings: Settings,
    device: str,
    isis: AgentResult,
    bgp: AgentResult,
    *,
    spine_only: bool = False,
    skip_llm: bool = False,
) -> AgentResult:
    seed = slice_topology_for_device(device, isis, bgp)

    # Deterministic hardware + CPU/mem spine; orchestrator merges into fleet maps
    hw_map = await collect_hardware_health(client, [device])
    hardware = hw_map.get(device) or {}
    sys_map = await collect_system_health(client, [device])
    system_health = sys_map.get(device) or {}
    seed = {**seed, "system_health": system_health}

    issues: list[dict[str, Any]] = []
    for issue in seed["isis"]["issues"] + seed["bgp"]["issues"]:
        row = dict(issue)
        row.setdefault("device", device)
        issues.append(row)
    if isinstance(hardware, dict) and hardware.get("error"):
        issues.append(
            {
                "severity": "medium",
                "layer": "device",
                "code": "hardware_collect_error",
                "edge_id": None,
                "device": device,
                "message": f"{device}: {hardware.get('error')}",
            }
        )
    if isinstance(system_health, dict) and system_health.get("error"):
        issues.append(
            {
                "severity": "low",
                "layer": "device",
                "code": "system_health_collect_error",
                "edge_id": None,
                "device": device,
                "message": f"{device}: {system_health.get('error')}",
            }
        )

    result = AgentResult(
        name=f"device:{device}",
        layer="device",
        issues=issues,
        seed=seed,
        hardware=hardware if isinstance(hardware, dict) else {},
        coverage={"devices_total": 1, "devices_queried": 1, "devices_failed": 0},
    )

    if spine_only or skip_llm or not settings.fabric_api_key:
        return result

    plan_raw = await _llm_plan(settings, device, seed, hardware)
    gated = gate_plan(
        plan_raw,
        allowlist=DEVICE_ALLOWLIST,
        device_names={device},
        force_device=device,
        max_tasks=5,
    )
    result.plan = gated
    if gated:
        result.evidence = await run_deep_checks(client, gated)
    result.narrative = await _llm_device_blurb(settings, device, result)
    return result


def format_device_section(result: AgentResult) -> str:
    device = (result.seed or {}).get("device") or result.name.removeprefix("device:")
    lines = [f"### {device}", ""]
    seed = result.seed or {}
    isis_n = len((seed.get("isis") or {}).get("operational_edges") or [])
    bgp_n = len((seed.get("bgp") or {}).get("operational_edges") or [])
    lines.append(f"Topology seed: IS-IS edges={isis_n}, BGP edges={bgp_n}")
    hw = result.hardware or {}
    if hw.get("error"):
        lines.append(f"Hardware: error — {hw.get('error')}")
    elif hw:
        outcome = hw.get("outcome") or hw.get("summary") or "collected"
        lines.append(f"Hardware: {outcome}")
    else:
        lines.append("Hardware: (none)")
    sys = (seed.get("system_health") or {}) if isinstance(seed, dict) else {}
    if isinstance(sys, dict) and sys:
        if sys.get("error"):
            lines.append(f"System health: error — {sys.get('error')}")
        else:
            cpu = (sys.get("cpu") or {}).get("one_min")
            mem = sys.get("memory") or {}
            mem_bits = []
            if mem.get("total_mb") is not None:
                mem_bits.append(f"mem {mem.get('available_mb')}/{mem.get('total_mb')} MB avail")
            cpu_bit = f"cpu1m={cpu}%" if cpu is not None else None
            detail = ", ".join(x for x in [cpu_bit, *mem_bits] if x) or "collected"
            lines.append(f"System health: {detail}")
    lines.append("")
    if result.issues:
        lines.append(f"Issues ({len(result.issues)}):")
        for issue in result.issues:
            lines.append(
                f"- ({issue.get('severity', '?')}) {issue.get('code')}: "
                f"{issue.get('message', '')}"
            )
    else:
        lines.append("No device-scoped issues from topology/hardware spine.")
    if result.narrative:
        lines.append("")
        lines.append(result.narrative.strip())
    if result.evidence:
        lines.append("")
        lines.extend(format_evidence_preview(result.evidence))
    lines.append("")
    return "\n".join(lines)


async def _llm_plan(
    settings: Settings,
    device: str,
    seed: dict[str, Any],
    hardware: dict[str, Any],
) -> list[dict[str, Any]]:
    from agent.summarize import fabric_openai_client

    prompt_path = _PROMPTS / "device_plan.txt"
    system = (
        prompt_path.read_text(encoding="utf-8")
        if prompt_path.is_file()
        else "Propose JSON MCP tasks for this device only."
    )
    user = json.dumps(
        {
            "device": device,
            "topology_seed": {
                "isis_issues": (seed.get("isis") or {}).get("issues"),
                "bgp_issues": (seed.get("bgp") or {}).get("issues"),
                "isis_edge_ids": [
                    e.get("id")
                    for e in (seed.get("isis") or {}).get("operational_edges") or []
                ][:20],
                "bgp_edge_ids": [
                    e.get("id")
                    for e in (seed.get("bgp") or {}).get("operational_edges") or []
                ][:20],
            },
            "hardware": {
                k: hardware.get(k)
                for k in ("outcome", "summary", "error")
                if k in hardware
            },
            "allowlist": sorted(DEVICE_ALLOWLIST),
            "note": "BGP/ISIS status already known from topology — do not re-derive.",
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


async def _llm_device_blurb(
    settings: Settings,
    device: str,
    result: AgentResult,
) -> str:
    from agent.summarize import fabric_openai_client

    prompt_path = _PROMPTS / "device_summary.txt"
    system = (
        prompt_path.read_text(encoding="utf-8")
        if prompt_path.is_file()
        else f"Short ops note for {device}. Cite only provided facts."
    )
    user = json.dumps(
        {
            "device": device,
            "issues": result.issues[:20],
            "hardware": {
                k: result.hardware.get(k)
                for k in ("outcome", "summary", "error")
                if k in (result.hardware or {})
            },
            "evidence_count": len(result.evidence),
            "evidence_preview": result.evidence[:3],
        },
        default=str,
    )
    try:
        client = fabric_openai_client(settings)
        resp = client.chat.completions.create(
            model=settings.fabric_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
        return (resp.choices[0].message.content or "").strip() if resp.choices else ""
    except Exception:  # noqa: BLE001
        return ""
