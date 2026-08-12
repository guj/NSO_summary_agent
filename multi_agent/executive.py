"""Executive-style header for multi-agent reports (Overall Status at top)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from nso_report.executive import (
    fleet_routing_totals,
    format_executive_section,
)
from multi_agent.base import AgentResult

_I1 = "  "
_I2 = "    "


def _marker(kind: str) -> str:
    return {"ok": "🟢", "attn": "🟡", "warn": "🔴"}.get(kind, "🟡")


def _parse_run_ts(run_id: str | None) -> str:
    if not run_id:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        dt = datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return run_id


def iso_run_id(run_id: str | None) -> str:
    """Compact ``YYYYMMDDTHHMMSSZ`` → ISO for production executive formatter."""
    if not run_id:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        dt = datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return run_id


def _find(results: list[AgentResult], layer: str) -> AgentResult | None:
    for r in results:
        if r.layer == layer:
            return r
    return None


def topology_view_from_agents(
    isis: AgentResult | None,
    bgp: AgentResult | None,
) -> dict[str, Any]:
    """Minimal topology blob so fleet_routing_totals matches production."""
    return {
        "static": {
            "layers": {
                "underlay": {"edges": list(isis.static_edges) if isis else []},
                "routing": {"edges": list(bgp.static_edges) if bgp else []},
            }
        },
        "operational": {
            "layers": {
                "underlay": {"edges": list(isis.operational_edges) if isis else []},
                "routing": {"edges": list(bgp.operational_edges) if bgp else []},
            }
        },
    }


def _routing_lines(
    isis: AgentResult | None,
    bgp: AgentResult | None,
) -> tuple[tuple[str, str], tuple[str, str], int, int, int, int]:
    """Return (isis_status, bgp_status, bgp_up, bgp_total, isis_up, isis_total).

    Fleet up/total use the same per-device sum as production Fleet Summary
    (each bidirectional edge counts once per endpoint device).
    """
    topo = topology_view_from_agents(isis, bgp)
    bgp_up, bgp_total, isis_up, isis_total = fleet_routing_totals(topo)

    isis_op = (isis.operational_summary if isis else {}) or {}
    bgp_op = (bgp.operational_summary if bgp else {}) or {}
    isis_issues = isis.issues if isis else []
    bgp_issues = bgp.issues if bgp else []
    uni = int(isis_op.get("unidirectional") or 0)
    deg = int(bgp_op.get("degraded") or 0)
    down_i = int(isis_op.get("down") or 0)
    down_b = int(bgp_op.get("down") or 0)

    if isis_total == 0:
        i_status = ("attn", "IS-IS: no adjacencies in scope")
    elif uni or down_i or any(
        i.get("code") == "unidirectional_adjacency" for i in isis_issues
    ):
        kind = "warn" if (uni or down_i or isis_up < isis_total) else "attn"
        extra = f" ({uni} unidirectional)" if uni else ""
        i_status = (kind, f"IS-IS: {isis_up}/{isis_total} up{extra}")
    elif isis_up == isis_total:
        i_status = ("ok", f"IS-IS: {isis_up}/{isis_total} up — healthy")
    else:
        i_status = ("attn", f"IS-IS: {isis_up}/{isis_total} up")

    if bgp_total == 0:
        b_status = ("attn", "BGP: no sessions in scope")
    elif down_b or deg or bgp_up < bgp_total or any(
        i.get("code") in {"missing_reverse_session", "unpaired_bgp_config"}
        for i in bgp_issues
    ):
        kind = "warn" if down_b or bgp_up < bgp_total else "attn"
        extra = f" ({deg} degraded)" if deg else ""
        b_status = (kind, f"BGP: {bgp_up}/{bgp_total} established{extra}")
    elif bgp_up == bgp_total:
        b_status = ("ok", f"BGP: {bgp_up}/{bgp_total} established — healthy")
    else:
        b_status = ("attn", f"BGP: {bgp_up}/{bgp_total} established")

    return i_status, b_status, bgp_up, bgp_total, isis_up, isis_total


def _device_status(devices: list[AgentResult]) -> tuple[str, str]:
    if not devices:
        return (
            "ok",
            "Devices: DeviceAgent skipped (no edge_id-linked ISIS/BGP issues; "
            "use --devices / --all-devices)",
        )
    with_issues = sum(1 for d in devices if d.issues)
    hw_err = sum(
        1 for d in devices if isinstance(d.hardware, dict) and d.hardware.get("error")
    )
    if hw_err or with_issues:
        kind = "warn" if hw_err else "attn"
        return (
            kind,
            f"Devices: {with_issues}/{len(devices)} with issues"
            + (f", {hw_err} hardware collect errors" if hw_err else ""),
        )
    return "ok", f"Devices: {len(devices)} investigated — no device-scoped issues"


def _severity_rank(sev: Any) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(str(sev).lower(), 9)


def build_action_items(
    results: list[AgentResult],
    *,
    limit: int = 8,
    fleet_pack: dict[str, Any] | None = None,
) -> list[str]:
    """Deterministic Action Items from high/medium ISIS/BGP/device issues.

    Inventory / hardware Review stay in Device Health + Detailed Analysis only —
    do not mirror them here (avoids a second copy of the same findings).
    ``fleet_pack`` is accepted for call-site compatibility but unused.
    """
    del fleet_pack  # reserved; inventory/HW are not Action Items
    items: list[tuple[int, str]] = []
    seen: set[str] = set()
    for result in results:
        for issue in result.issues:
            if not isinstance(issue, dict):
                continue
            sev = str(issue.get("severity") or "low").lower()
            if sev not in {"high", "medium"}:
                continue
            code = str(issue.get("code") or "issue")
            msg = str(issue.get("message") or "").strip()
            key = f"{code}:{msg}"
            if key in seen:
                continue
            seen.add(key)
            line = f"{msg} ({code})" if msg else code
            items.append((_severity_rank(sev), line))
    items.sort(key=lambda x: x[0])
    return [t[1] for t in items][:limit]


def _overall_narrative(
    results: list[AgentResult],
    *,
    include_services_note: bool = True,
    fleet_pack: dict[str, Any] | None = None,
) -> str:
    """Deterministic Overall Status + Action Items for production formatter."""
    isis = _find(results, "underlay")
    bgp = _find(results, "routing")
    devices = [r for r in results if r.layer == "device"]

    i_status, b_status, _, _, _, _ = _routing_lines(isis, bgp)
    i_kind, i_line = i_status
    b_kind, b_line = b_status
    d_kind, d_line = _device_status(devices)

    routing_kinds = {i_kind, b_kind}
    if "warn" in routing_kinds:
        r_mark = "warn"
    elif "attn" in routing_kinds:
        r_mark = "attn"
    else:
        r_mark = "ok"
    routing_line = (
        f"Routing: {i_line.replace('IS-IS: ', 'IS-IS ')} ; "
        f"{b_line.replace('BGP: ', 'BGP ')}"
    )

    lines = [
        "Overall Status",
        "--------------",
        f"{_I1}{_marker(r_mark)} {routing_line}",
        f"{_I1}{_marker(d_kind)} {d_line}",
    ]
    if include_services_note:
        lines.append(
            f"{_I1}(Services / fleet sync / inventory review: not collected in this multi-agent run)"
        )
    lines.extend(["", "Action Items", "------------"])
    actions = build_action_items(results, fleet_pack=fleet_pack)
    if not actions:
        lines.append(f"{_I1}None — no high/medium issues from this run.")
    else:
        for n, action in enumerate(actions, start=1):
            lines.append(f"{_I1}{n}. {action}")
    return "\n".join(lines)


def format_executive_header(
    results: list[AgentResult],
    *,
    run_id: str | None = None,
    summary_text: str | None = None,
    fleet_pack: dict[str, Any] | None = None,
    fabric_api_url: str | None = None,
    fabric_model: str | None = None,
    mcp_server_cmd: str | None = None,
    llm_skipped: bool = False,
) -> str:
    """Overall Status + Fleet Summary + Action Items (+ optional full fleet sections).

    When ``fleet_pack`` is present (services, sync, CPU, hardware, topology, delta),
    reuse production ``format_executive_section`` so Service Summary, Device Health,
    Changes Since Last Report, and fleet sync/CPU/inventory match the main CLI.
    """
    provenance = {
        "fabric_api_url": fabric_api_url,
        "fabric_model": fabric_model,
        "mcp_server_cmd": mcp_server_cmd,
        "llm_skipped": llm_skipped,
    }
    if fleet_pack is not None:
        narrative = _overall_narrative(
            results, include_services_note=False, fleet_pack=fleet_pack
        )
        assessment = (summary_text or "").strip() or "None reported."
        return format_executive_section(
            run_id=iso_run_id(run_id),
            counts=fleet_pack.get("counts") or {},
            topology=fleet_pack.get("topology"),
            delta=fleet_pack.get("delta")
            or {"first_run": True, "counts": {}, "new_failures": [], "recoveries": []},
            llm_narrative=narrative,
            rich_markers=True,
            fleet_sync=fleet_pack.get("fleet_sync"),
            system_health=fleet_pack.get("system_health"),
            hardware_health=fleet_pack.get("hardware_health"),
            operational_assessment=assessment,
            title="NSO Multi-Agent Snapshot",
            **provenance,
        )

    isis = _find(results, "underlay")
    bgp = _find(results, "routing")
    devices = [r for r in results if r.layer == "device"]

    i_status, b_status, bgp_up, bgp_total, isis_up, isis_total = _routing_lines(
        isis, bgp
    )
    i_kind, i_line = i_status
    b_kind, b_line = b_status
    d_kind, d_line = _device_status(devices)

    routing_kinds = {i_kind, b_kind}
    if "warn" in routing_kinds:
        r_mark = "warn"
    elif "attn" in routing_kinds:
        r_mark = "attn"
    else:
        r_mark = "ok"
    routing_line = (
        f"Routing: {i_line.replace('IS-IS: ', 'IS-IS ')} ; "
        f"{b_line.replace('BGP: ', 'BGP ')}"
    )

    isis_op = (isis.operational_summary if isis else {}) or {}
    uni = int(isis_op.get("unidirectional") or 0)

    from nso_report.executive import format_run_provenance_lines

    lines: list[str] = [
        "=====================================================",
        "NSO Multi-Agent Snapshot",
        _parse_run_ts(run_id),
        *format_run_provenance_lines(
            fabric_api_url=fabric_api_url,
            fabric_model=fabric_model,
            mcp_server_cmd=mcp_server_cmd,
            llm_skipped=llm_skipped,
        ),
        "=====================================================",
        "",
        "Overall Status",
        "--------------",
        f"{_I1}{_marker(r_mark)} {routing_line}",
        f"{_I1}{_marker(d_kind)} {d_line}",
        f"{_I1}(Services / fleet sync / inventory review: not collected in this multi-agent run)",
        "",
        "Fleet Summary",
        "-------------",
        f"{_I1}Routing",
        f"{_I2}BGP Peers: {bgp_up}/{bgp_total} Established",
        (
            f"{_I2}IS-IS Adjacencies: {isis_up}/{isis_total} Up"
            + (f" ({uni} unidirectional unique edges)" if uni else "")
        ),
    ]
    if devices:
        with_issues = sum(1 for d in devices if d.issues)
        lines.append(f"{_I1}Devices (investigated)")
        lines.append(f"{_I2}Total: {len(devices)}")
        lines.append(f"{_I2}With issues: {with_issues}")
    lines.extend(["", "Action Items", "------------"])
    actions = build_action_items(results, fleet_pack=None)
    if not actions:
        lines.append(f"{_I1}None — no high/medium issues from this run.")
    else:
        for n, action in enumerate(actions, start=1):
            lines.append(f"{_I1}{n}. {action}")

    if summary_text and summary_text.strip():
        lines.extend(
            [
                "",
                "Operational Assessment",
                "----------------------",
                f"{_I1}{summary_text.strip().replace(chr(10), chr(10) + _I1)}",
            ]
        )

    lines.append("")
    return "\n".join(lines)
