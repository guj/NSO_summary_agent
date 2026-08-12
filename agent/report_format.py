"""Deterministic report sections (stable layout across runs)."""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent.report_devices import format_devices_section
from nso_report.delta_format import delta_has_changes, format_delta_section

_LEGACY_SECTIONS: tuple[str, ...] = (
    "problems",
    "counts",
    "delta",
    "fleet_sync",
    "ignored_types",
)

_DETAILS_BANNER = (
    "=====================================================\n"
    "Detailed Device Analysis\n"
    "====================================================="
)

_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")


@dataclass(frozen=True)
class ReportOutputs:
    """Report text tuned per channel (markdown tables do not render in Slack/email)."""

    markdown: str
    plain: str
    html: str


def format_service_counts_table(counts: dict[str, Any]) -> str:
    """Markdown table: one row per service type."""
    header = "| Type | Total | Up | Down | Degraded | Unknown |"
    separator = "| --- | ---: | ---: | ---: | ---: | ---: |"
    if not counts:
        return "\n".join([header, separator, "| (none) | 0 | 0 | 0 | 0 | 0 |"])

    rows = [header, separator]
    for service_type in sorted(counts):
        bucket = counts[service_type] or {}
        rows.append(
            "| {type} | {total} | {up} | {down} | {degraded} | {unknown} |".format(
                type=service_type,
                total=_int(bucket, "total"),
                up=_int(bucket, "up"),
                down=_int(bucket, "down"),
                degraded=_int(bucket, "degraded"),
                unknown=_int(bucket, "unknown"),
            )
        )
    return "\n".join(rows)


def format_service_counts_plain(counts: dict[str, Any]) -> str:
    """Fixed-width text table for Slack, email, and terminals."""
    header = f"{'Type':<14}{'Total':>6}{'Up':>5}{'Down':>6}{'Degr':>6}{'Unkn':>6}"
    sep = "-" * len(header)
    if not counts:
        return f"{header}\n{sep}\n{'(none)':<14}{0:>6}{0:>5}{0:>6}{0:>6}{0:>6}"

    lines = [header, sep]
    for service_type in sorted(counts):
        bucket = counts[service_type] or {}
        lines.append(
            f"{service_type:<14}"
            f"{_int(bucket, 'total'):>6}"
            f"{_int(bucket, 'up'):>5}"
            f"{_int(bucket, 'down'):>6}"
            f"{_int(bucket, 'degraded'):>6}"
            f"{_int(bucket, 'unknown'):>6}"
        )
    return "\n".join(lines)


def format_service_counts_html(counts: dict[str, Any]) -> str:
    rows = []
    for service_type in sorted(counts):
        bucket = counts[service_type] or {}
        rows.append(
            "<tr>"
            f"<td>{html.escape(service_type)}</td>"
            f"<td>{_int(bucket, 'total')}</td>"
            f"<td>{_int(bucket, 'up')}</td>"
            f"<td>{_int(bucket, 'down')}</td>"
            f"<td>{_int(bucket, 'degraded')}</td>"
            f"<td>{_int(bucket, 'unknown')}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(
            "<tr><td>(none)</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td></tr>"
        )
    return (
        "<table border='1' cellpadding='4' cellspacing='0'>"
        "<tr><th>Type</th><th>Total</th><th>Up</th><th>Down</th>"
        "<th>Degraded</th><th>Unknown</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def format_fleet_sync_summary(fleet_sync: dict[str, Any] | None) -> str:
    if not isinstance(fleet_sync, dict) or fleet_sync.get("status") != "success":
        return "Fleet sync: unavailable"

    data = fleet_sync.get("data") or {}
    summary = data.get("summary") or {}
    devices = data.get("devices") or []
    in_sync = _int(summary, "in_sync")
    out_of_sync = _int(summary, "out_of_sync")
    error = _int(summary, "error")
    device_count = len(devices) if devices else in_sync + out_of_sync + error

    if out_of_sync == 0 and error == 0:
        label = "device" if device_count == 1 else "devices"
        return f"All {device_count} {label} are in-sync"

    lines = [
        f"Fleet sync: {in_sync} in-sync, {out_of_sync} out-of-sync, {error} error"
    ]
    for row in devices:
        if not isinstance(row, dict):
            continue
        result = str(row.get("result", ""))
        if result != "in-sync":
            lines.append(f"- {row.get('device')}: {result}")
    return "\n".join(lines)


def assemble_report(
    *,
    run_id: str,
    problems: str,
    counts_table: str,
    delta_section: str,
    fleet_sync_section: str,
    ignored_types: list[str] | None = None,
    sections: Sequence[str] = _LEGACY_SECTIONS,
    devices_body: str = "No device topology in snapshot.",
    system_health: str = "None reported.",
) -> str:
    ctx = _section_ctx(
        problems=problems,
        counts_md=counts_table,
        counts_plain="",
        counts_html="",
        delta_section=delta_section,
        fleet_sync_section=fleet_sync_section,
        devices_body=devices_body,
        system_health=system_health,
        ignored_types=ignored_types,
    )
    return _assemble_markdown(run_id, sections, ctx)


def assemble_report_plain(
    *,
    run_id: str,
    problems: str,
    counts_block: str,
    delta_section: str,
    fleet_sync_section: str,
    ignored_types: list[str] | None = None,
    sections: Sequence[str] = _LEGACY_SECTIONS,
    devices_body: str = "No device topology in snapshot.",
    system_health: str = "None reported.",
) -> str:
    ctx = _section_ctx(
        problems=problems,
        counts_md="",
        counts_plain=counts_block,
        counts_html="",
        delta_section=delta_section,
        fleet_sync_section=fleet_sync_section,
        devices_body=devices_body,
        system_health=system_health,
        ignored_types=ignored_types,
    )
    return _assemble_plain(run_id, sections, ctx)


def assemble_report_html(
    *,
    run_id: str,
    problems: str,
    counts_html: str,
    delta_section: str,
    fleet_sync_section: str,
    ignored_types: list[str] | None = None,
    sections: Sequence[str] = _LEGACY_SECTIONS,
    devices_body: str = "No device topology in snapshot.",
    system_health: str = "None reported.",
) -> str:
    ctx = _section_ctx(
        problems=problems,
        counts_md="",
        counts_plain="",
        counts_html=counts_html,
        delta_section=delta_section,
        fleet_sync_section=fleet_sync_section,
        devices_body=devices_body,
        system_health=system_health,
        ignored_types=ignored_types,
    )
    return _assemble_html(run_id, sections, ctx)


def build_reports(
    *,
    run_id: str,
    problems: str,
    counts: dict[str, Any],
    delta_section: str,
    fleet_sync_section: str,
    ignored_types: list[str] | None = None,
    sections: Sequence[str],
    topology: Any = None,
    system_health: str = "None reported.",
    executive_narrative: str = "None reported.",
    operational_assessment: str = "None reported.",
    delta: dict[str, Any] | None = None,
    fleet_sync: Any = None,
    system_health_data: Any = None,
    hardware_health: Any = None,
    services: dict[str, Any] | None = None,
    fabric_api_url: str | None = None,
    fabric_model: str | None = None,
    mcp_server_cmd: str | None = None,
    llm_skipped: bool = False,
) -> ReportOutputs:
    from agent.report_executive import format_executive_section

    devices_body = format_devices_section(
        topology,
        services=services,
        fleet_sync=fleet_sync,
        hardware_health=hardware_health,
    )
    delta_obj = delta if isinstance(delta, dict) else {}
    provenance = {
        "fabric_api_url": fabric_api_url,
        "fabric_model": fabric_model,
        "mcp_server_cmd": mcp_server_cmd,
        "llm_skipped": llm_skipped,
    }
    executive_md = format_executive_section(
        run_id=run_id,
        counts=counts,
        topology=topology,
        delta=delta_obj,
        llm_narrative=executive_narrative,
        rich_markers=True,
        fleet_sync=fleet_sync,
        system_health=system_health_data,
        hardware_health=hardware_health,
        operational_assessment=operational_assessment,
        **provenance,
    )
    executive_plain = format_executive_section(
        run_id=run_id,
        counts=counts,
        topology=topology,
        delta=delta_obj,
        llm_narrative=executive_narrative,
        rich_markers=False,
        fleet_sync=fleet_sync,
        system_health=system_health_data,
        hardware_health=hardware_health,
        operational_assessment=operational_assessment,
        **provenance,
    )
    ctx = _section_ctx(
        problems=problems,
        counts_md=format_service_counts_table(counts),
        counts_plain=format_service_counts_plain(counts),
        counts_html=format_service_counts_html(counts),
        delta_section=delta_section,
        fleet_sync_section=fleet_sync_section,
        devices_body=devices_body,
        system_health=system_health,
        ignored_types=ignored_types,
        executive_md=executive_md,
        executive_plain=executive_plain,
    )
    return ReportOutputs(
        markdown=_assemble_markdown(run_id, sections, ctx),
        plain=_assemble_plain(run_id, sections, ctx),
        html=_assemble_html(run_id, sections, ctx),
    )


def _section_ctx(
    *,
    problems: str,
    counts_md: str,
    counts_plain: str,
    counts_html: str,
    delta_section: str,
    fleet_sync_section: str,
    devices_body: str,
    system_health: str,
    ignored_types: list[str] | None,
    executive_md: str = "",
    executive_plain: str = "",
) -> dict[str, Any]:
    return {
        "problems": problems.strip() or "None reported.",
        "counts_md": counts_md,
        "counts_plain": counts_plain,
        "counts_html": counts_html,
        "delta": delta_section,
        "fleet": fleet_sync_section.strip(),
        "system_health": (system_health or "").strip() or "None reported.",
        "devices": devices_body,
        "ignored": ignored_types or [],
        "executive_md": executive_md,
        "executive_plain": executive_plain,
    }


def _assemble_markdown(run_id: str, sections: Sequence[str], ctx: dict[str, Any]) -> str:
    parts: list[str] = []
    if "executive" not in sections:
        parts.append(f"**NSO Ops Snapshot — {run_id}**")
    for name in sections:
        if name == "executive":
            parts.append(ctx.get("executive_md") or "")
        elif name == "problems":
            parts.append(f"**Problems / Failures**\n{ctx['problems']}")
        elif name == "counts":
            parts.append(f"**Service Counts (by type)**\n{ctx['counts_md']}")
        elif name == "delta":
            parts.append(f"**Delta since last run**\n{ctx['delta']}")
        elif name == "fleet_sync":
            parts.append(f"**Fleet sync**\n{ctx['fleet']}")
        elif name == "system_health":
            parts.append(f"**Infrastructure Health**\n{ctx['system_health']}")
        elif name == "devices":
            parts.append(f"{_DETAILS_BANNER}\n\n{ctx['devices']}")
        elif name == "ignored_types":
            ignored = ctx["ignored"]
            if ignored:
                parts.append(f"**Ignored service types**\n{', '.join(ignored)}")
    return "\n\n".join(p for p in parts if p)


def _assemble_plain(run_id: str, sections: Sequence[str], ctx: dict[str, Any]) -> str:
    parts: list[str] = []
    if "executive" not in sections:
        parts.append(f"NSO Ops Snapshot — {run_id}\n{'=' * 40}")
    for name in sections:
        if name == "executive":
            parts.append(ctx.get("executive_plain") or "")
        elif name == "problems":
            parts.append(
                f"Problems / Failures\n{'-' * 20}\n{_strip_md_bold(ctx['problems'])}"
            )
        elif name == "counts":
            parts.append(
                f"Service Counts (by type)\n{'-' * 20}\n{ctx['counts_plain']}"
            )
        elif name == "delta":
            parts.append(
                f"Delta since last run\n{'-' * 20}\n{ctx['delta']}"
            )
        elif name == "fleet_sync":
            parts.append(f"Fleet sync\n{'-' * 20}\n{ctx['fleet']}")
        elif name == "system_health":
            parts.append(
                f"Infrastructure Health\n{'-' * 20}\n{_strip_md_bold(ctx['system_health'])}"
            )
        elif name == "devices":
            parts.append(
                f"{_DETAILS_BANNER}\n\n{_strip_md_bold(ctx['devices'])}"
            )
        elif name == "ignored_types":
            ignored = ctx["ignored"]
            if ignored:
                parts.append(f"Ignored service types: {', '.join(ignored)}")
    return "\n\n".join(p for p in parts if p)


def _assemble_html(run_id: str, sections: Sequence[str], ctx: dict[str, Any]) -> str:
    body_parts: list[str] = []
    if "executive" not in sections:
        body_parts.append(f"<h1>NSO Ops Snapshot — {html.escape(run_id)}</h1>")
    for name in sections:
        if name == "executive":
            body_parts.append(
                "<h2>Executive Summary</h2>"
                f"<pre>{_html_from_text(ctx.get('executive_md') or '')}</pre>"
            )
        elif name == "problems":
            body_parts.append(
                f"<h2>Problems / Failures</h2><p>{_html_from_text(ctx['problems'])}</p>"
            )
        elif name == "counts":
            body_parts.append(
                f"<h2>Service Counts (by type)</h2>{ctx['counts_html']}"
            )
        elif name == "delta":
            body_parts.append(
                f"<h2>Delta since last run</h2><p>{_html_from_text(ctx['delta'])}</p>"
            )
        elif name == "fleet_sync":
            body_parts.append(
                f"<h2>Fleet sync</h2><p>{_html_from_text(ctx['fleet'])}</p>"
            )
        elif name == "system_health":
            body_parts.append(
                f"<h2>Infrastructure Health</h2><p>{_html_from_text(ctx['system_health'])}</p>"
            )
        elif name == "devices":
            body_parts.append(
                f"<pre>{_html_from_text(_DETAILS_BANNER)}</pre>"
                f"<pre>{_html_from_text(ctx['devices'])}</pre>"
            )
        elif name == "ignored_types":
            ignored = ctx["ignored"]
            if ignored:
                body_parts.append(
                    "<h2>Ignored service types</h2>"
                    f"<p>{html.escape(', '.join(ignored))}</p>"
                )
    return "<html><body>" + "".join(body_parts) + "</body></html>"


def _strip_md_bold(text: str) -> str:
    """Remove ``**…**`` markers for plain Slack/email bodies."""
    return _MD_BOLD.sub(r"\1", text)


def _html_from_text(text: str) -> str:
    """Escape text, turn ``**bold**`` into ``<strong>``, keep newlines."""
    escaped = html.escape(text)
    with_bold = _MD_BOLD.sub(r"<strong>\1</strong>", escaped)
    return with_bold.replace("\n", "<br>\n")


def _int(bucket: dict[str, Any], key: str) -> int:
    try:
        return int(bucket.get(key, 0))
    except (TypeError, ValueError):
        return 0
