"""Summarize snapshot JSON via FABRIC AI (OpenAI-compatible API)."""

from __future__ import annotations

from agent.config import llm_temperature_kwargs

import json
from typing import Any

from openai import OpenAI

from agent.config import Settings, load_settings
from agent.report_format import (
    ReportOutputs,
    build_reports,
    format_delta_section,
    format_fleet_sync_summary,
)

# Bound Fabric/NRP chat calls. OpenAI SDK default is 600s and routinely
# sits 2–3+ minutes per plan turn with no progress logs.
FABRIC_CHAT_TIMEOUT_SEC = 60.0
# Default: one attempt, no automatic retries. Settings may override via env.
FABRIC_CHAT_MAX_RETRIES = 0


def llm_timeout_seconds(settings) -> float:
    return float(getattr(settings, "fabric_chat_timeout_sec", FABRIC_CHAT_TIMEOUT_SEC))


def llm_connect_timeout_seconds(settings) -> float:
    return float(getattr(settings, "fabric_chat_connect_timeout_sec", 20.0))


def llm_error_category(error: BaseException) -> str:
    """Classify the SDK cause chain, never guess from elapsed time."""
    import httpx
    seen = set()
    current = error
    chain = []
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    for cls, label in ((httpx.ConnectTimeout, "connection_timeout"),
                       (httpx.ReadTimeout, "response_read_timeout"),
                       (httpx.WriteTimeout, "request_write_timeout"),
                       (httpx.PoolTimeout, "connection_pool_timeout"),
                       (httpx.ConnectError, "connection_error")):
        if any(isinstance(exc, cls) for exc in chain):
            return label
    from openai import APITimeoutError, APIConnectionError
    if any(isinstance(exc, (APITimeoutError, TimeoutError, httpx.TimeoutException)) for exc in chain):
        return "timeout_unspecified"
    if isinstance(error, APIConnectionError):
        return "connection_error"
    return type(error).__name__


def fabric_openai_client(
    settings: Settings | None = None,
    *,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> OpenAI:
    """OpenAI-compatible client for FABRIC AI / substitute providers.

    ``FABRIC_AI_API_URL`` must be an OpenAI chat base (host root or ``…/v1``).
    Anthropic-only bases (e.g. ``…/anthropic``) are not supported.

    ``timeout`` sets read/write/pool inactivity limits, not total wall-clock time. Defaults to
    the settings value from ``FABRIC_CHAT_TIMEOUT_SEC`` (default 60s). ``max_retries`` defaults to
    the settings value from ``FABRIC_CHAT_MAX_RETRIES`` (default 0).
    Connection establishment uses FABRIC_CHAT_CONNECT_TIMEOUT_SEC (default 20s).
    Retries apply to all SDK-retryable errors, including timeouts.
    """
    s = settings or load_settings()
    base = s.fabric_api_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    import httpx

    bound = llm_timeout_seconds(s) if timeout is None else float(timeout)
    retries = (
        getattr(s, "fabric_max_retries", FABRIC_CHAT_MAX_RETRIES)
        if max_retries is None else int(max_retries)
    )
    kwargs: dict[str, Any] = {
        "api_key": s.fabric_api_key,
        "base_url": base,
        "timeout": httpx.Timeout(bound, connect=llm_connect_timeout_seconds(s)),
        "max_retries": retries,
    }
    return OpenAI(**kwargs)


def load_system_prompt(settings: Settings | None = None) -> str:
    s = settings or load_settings()
    path = s.prompts_dir / "summary_system.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return (
        "You are a network operations reporter for Cisco NSO-managed services. "
        "Write only the Problems / Failures section. Lead with problems. "
        "Do not invent data not in the JSON."
    )


def load_system_health_prompt(settings: Settings | None = None) -> str:
    s = settings or load_settings()
    path = s.prompts_dir / "system_health_system.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return (
        "Write only the Infrastructure Health section from the provided CPU/memory JSON. "
        "Do not invent numbers."
    )


def load_executive_prompt(settings: Settings | None = None) -> str:
    s = settings or load_settings()
    path = s.prompts_dir / "executive_system.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return (
        "Write Overall Status and Action Items from the JSON fact pack "
        "(prefer action_context). Do not invent data."
    )


def load_operational_assessment_prompt(settings: Settings | None = None) -> str:
    s = settings or load_settings()
    path = s.prompts_dir / "operational_assessment_system.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return (
        "Write one short Operational Assessment paragraph from the JSON facts. "
        "Do not invent data. No heading."
    )


def summarize_report(
    snapshot: dict[str, Any],
    delta: dict[str, Any],
    settings: Settings | None = None,
) -> ReportOutputs:
    s = settings or load_settings()
    sections = s.report_sections
    if "executive" in sections:
        executive_narrative = _summarize_executive(snapshot, delta, s)
        operational_assessment = _summarize_operational_assessment(
            snapshot, delta, s
        )
    else:
        executive_narrative = "None reported."
        operational_assessment = "None reported."

    if "problems" in sections:
        problems = _summarize_problems(snapshot, delta, s)
    else:
        problems = "None reported."

    if "system_health" in sections:
        system_health = _summarize_system_health(snapshot, s)
    else:
        system_health = "None reported."

    return build_reports(
        run_id=str(snapshot.get("run_id", "unknown")),
        problems=problems,
        counts=snapshot.get("counts") or {},
        delta_section=format_delta_section(delta),
        fleet_sync_section=format_fleet_sync_summary(snapshot.get("fleet_sync")),
        system_health=system_health,
        ignored_types=snapshot.get("ignored_service_types") or [],
        sections=sections,
        topology=snapshot.get("topology"),
        executive_narrative=executive_narrative,
        operational_assessment=operational_assessment,
        delta=delta,
        fleet_sync=snapshot.get("fleet_sync"),
        system_health_data=snapshot.get("system_health") or {},
        hardware_health=snapshot.get("hardware_health") or {},
        services=snapshot.get("services") or {},
        fabric_api_url=s.fabric_api_url,
        fabric_model=s.fabric_model,
        mcp_server_cmd=s.mcp_server_cmd,
        llm_skipped=False,
    )


def _summarize_executive(
    snapshot: dict[str, Any],
    delta: dict[str, Any],
    settings: Settings,
) -> str:
    from agent.report_executive import executive_fact_pack

    client = fabric_openai_client(settings)
    payload = executive_fact_pack(snapshot, delta)
    response = client.chat.completions.create(
        model=settings.fabric_model,
        messages=[
            {"role": "system", "content": load_executive_prompt(settings)},
            {
                "role": "user",
                "content": (
                    "Write Overall Status and Action Items only.\n\n"
                    f"{json.dumps(payload, indent=2, default=str)}"
                ),
            },
        ],
        **llm_temperature_kwargs(settings),
    )
    text = (response.choices[0].message.content or "None reported.").strip()
    return text or "None reported."


def _summarize_operational_assessment(
    snapshot: dict[str, Any],
    delta: dict[str, Any],
    settings: Settings,
) -> str:
    from agent.report_executive import executive_fact_pack, format_fleet_summary

    client = fabric_openai_client(settings)
    payload = executive_fact_pack(snapshot, delta)
    payload["fleet_summary_text"] = format_fleet_summary(
        counts=snapshot.get("counts") or {},
        topology=snapshot.get("topology"),
        fleet_sync=snapshot.get("fleet_sync"),
        system_health=snapshot.get("system_health") or {},
        hardware_health=snapshot.get("hardware_health") or {},
    )
    response = client.chat.completions.create(
        model=settings.fabric_model,
        messages=[
            {
                "role": "system",
                "content": load_operational_assessment_prompt(settings),
            },
            {
                "role": "user",
                "content": (
                    "Write the Operational Assessment body only "
                    "(one paragraph, no heading).\n\n"
                    f"{json.dumps(payload, indent=2, default=str)}"
                ),
            },
        ],
        **llm_temperature_kwargs(settings),
    )
    text = (response.choices[0].message.content or "None reported.").strip()
    text = _strip_section_heading(text, ("Operational Assessment",))
    return text or "None reported."


def _summarize_problems(
    snapshot: dict[str, Any],
    delta: dict[str, Any],
    settings: Settings,
) -> str:
    client = fabric_openai_client(settings)
    payload = {
        "snapshot": _problems_context(snapshot),
        "delta": {
            "new_failures": delta.get("new_failures"),
            "recoveries": delta.get("recoveries"),
            "status_changes": delta.get("status_changes"),
        },
    }
    response = client.chat.completions.create(
        model=settings.fabric_model,
        messages=[
            {"role": "system", "content": load_system_prompt(settings)},
            {
                "role": "user",
                "content": (
                    "Write the Problems / Failures body only "
                    "(no section title — the report adds the heading).\n\n"
                    f"{json.dumps(payload, indent=2, default=str)}"
                ),
            },
        ],
        **llm_temperature_kwargs(settings),
    )
    return _strip_section_heading(
        (response.choices[0].message.content or "None reported.").strip(),
        ("Problems / Failures", "Problems", "Failures"),
    )


def _summarize_system_health(
    snapshot: dict[str, Any],
    settings: Settings,
) -> str:
    health = snapshot.get("system_health") or {}
    if not health:
        return "None reported."
    client = fabric_openai_client(settings)
    payload = {"run_id": snapshot.get("run_id"), "system_health": health}
    response = client.chat.completions.create(
        model=settings.fabric_model,
        messages=[
            {"role": "system", "content": load_system_health_prompt(settings)},
            {
                "role": "user",
                "content": (
                    "Write the Infrastructure Health body only "
                    "(no section title — the report adds the heading).\n\n"
                    f"{json.dumps(payload, indent=2, default=str)}"
                ),
            },
        ],
        **llm_temperature_kwargs(settings),
    )
    return _strip_section_heading(
        (response.choices[0].message.content or "None reported.").strip(),
        ("Infrastructure Health", "System Health"),
    )


def _strip_section_heading(text: str, titles: tuple[str, ...]) -> str:
    """Remove a leading section title the LLM may echo (report already titles it)."""
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return text
    first = lines[0].strip()
    normalized = first.strip("*#_ ").rstrip(":").strip()
    if normalized in titles:
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
        return "\n".join(lines).strip() or "None reported."
    return text


def _problems_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Trim snapshot to what the LLM needs for the problems narrative."""
    services = snapshot.get("services") or {}
    problem_instances = {
        name: record
        for name, record in services.items()
        if isinstance(record, dict)
        and record.get("status") in ("down", "degraded", "unknown")
    }
    return {
        "run_id": snapshot.get("run_id"),
        "counts": snapshot.get("counts"),
        "problem_instances": problem_instances,
        "fleet_sync": snapshot.get("fleet_sync"),
        "interface_mismatches": _interface_mismatch_issues(snapshot),
    }


def _interface_mismatch_issues(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Physical NSO↔box mismatch issues for the Problems narrative."""
    topology = snapshot.get("topology") or {}
    if not isinstance(topology, dict):
        return []
    operational = topology.get("operational") or {}
    if not isinstance(operational, dict):
        return []
    codes = {"config_live_mismatch", "equivalence_ignored"}
    out: list[dict[str, Any]] = []
    for issue in operational.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        if issue.get("code") not in codes:
            continue
        out.append(
            {
                "code": issue.get("code"),
                "device": issue.get("device"),
                "nso": issue.get("nso"),
                "candidates": issue.get("candidates"),
                "box": issue.get("box"),
                "kind": issue.get("kind"),
                "confirmed": issue.get("confirmed"),
                "message": issue.get("message"),
            }
        )
    return out
