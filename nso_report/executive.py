"""Executive Summary section: Python tables + injected LLM narrative."""

from __future__ import annotations

import re
from typing import Any

from nso_facts.fleet_summary_thresholds import (
    FleetSummaryThresholds,
)
from nso_facts.health import build_device_sync_map
from nso_facts.fleet_rollups import (
    fleet_device_sync_counts,
    fleet_infra_alert_counts,
    fleet_routing_totals,
    fleet_service_instance_counts,
)
from nso_report.delta_format import delta_has_changes, format_delta_section
from nso_report.devices import (
    INVENTORY_REVIEW_STATUS,
    _devices_in_edges,
    _iface_pair_status,
    _merged_edges,
    _mismatch_by_edge_id,
    _operational_layers,
    _peer_up_total,
    _route_summaries,
    _static_layers,
    _status,
    _unmapped_by_device,
)

_I1 = "  "
_I2 = "    "

_MARKER_PLAIN = {"ok": "OK", "attn": "ATTN", "warn": "WARN"}
_MARKER_RICH = {"ok": "🟢", "attn": "🟡", "warn": "🔴"}
_EMOJI_TO_PLAIN = {
    "🟢": "OK",
    "🟡": "ATTN",
    "🔴": "WARN",
}


def marker(kind: str, *, rich: bool) -> str:
    table = _MARKER_RICH if rich else _MARKER_PLAIN
    return table.get(kind, table["attn"])


def replace_markers_for_plain(text: str) -> str:
    out = text
    for emoji, label in _EMOJI_TO_PLAIN.items():
        out = out.replace(emoji, label)
    return out


def format_service_summary_lines(counts: dict[str, Any]) -> list[str]:
    if not counts:
        return [f"{_I1}(none)"]
    lines: list[str] = []
    for service_type in sorted(counts):
        bucket = counts[service_type] or {}
        try:
            total = int(bucket.get("total", 0))
            up = int(bucket.get("up", 0))
        except (TypeError, ValueError):
            total, up = 0, 0
        lines.append(f"{_I1}{service_type:<14}{up}/{total} Up")
    return lines


def abbreviate_route_total(total: Any) -> str:
    if total is None:
        return "—"
    try:
        n = int(total)
    except (TypeError, ValueError):
        return str(total)
    if n >= 1000:
        k = n / 1000.0
        if abs(k - round(k)) < 0.05:
            return f"{int(round(k))}k"
        return f"{k:.0f}k" if k >= 10 else f"{k:.1f}k".rstrip("0").rstrip(".") + "k"
    return str(n)


def device_interfaces_label(
    device: str,
    phys: list,
    mismatch_by_edge: dict[str, dict[str, Any]],
) -> str:
    for e in phys:
        if not isinstance(e, dict):
            continue
        if (e.get("local") or {}).get("device") != device:
            continue
        pair = _iface_pair_status(e)
        if pair in {"up/down", "unknown"}:
            return INVENTORY_REVIEW_STATUS
        edge_id = str(e.get("id") or "")
        issue = mismatch_by_edge.get(edge_id)
        if issue and not issue.get("confirmed"):
            return INVENTORY_REVIEW_STATUS
    # Also: mismatch issues for device without edge join
    for issue in mismatch_by_edge.values():
        if issue.get("confirmed"):
            continue
        if issue.get("device") == device:
            return INVENTORY_REVIEW_STATUS
    return "Healthy"


def device_notes_label(
    device: str,
    phys: list,
    mismatch_by_edge: dict[str, dict[str, Any]],
) -> str:
    """Short Device Health notes — skip admin-down / quiet down/down.

    Notes call out items that need attention (aligned with Interfaces=Inventory Review):
    ``up/down``, ``unknown``, or unconfirmed NSO↔box mismatches.
    """
    has_up_down = False
    has_unknown = False
    for e in phys:
        if not isinstance(e, dict):
            continue
        if (e.get("local") or {}).get("device") != device:
            continue
        pair = _iface_pair_status(e)
        if pair == "up/down":
            has_up_down = True
        elif pair == "unknown":
            has_unknown = True

    has_mismatch = False
    for edge_id, issue in mismatch_by_edge.items():
        if issue.get("confirmed"):
            continue
        if issue.get("device") == device:
            has_mismatch = True
            break
        # Also tie by edge when local device matches
        for e in phys:
            if not isinstance(e, dict):
                continue
            if str(e.get("id") or "") == edge_id and (
                e.get("local") or {}
            ).get("device") == device:
                has_mismatch = True
                break

    parts: list[str] = []
    if has_up_down:
        parts.append("Interface up/down")
    if has_unknown or has_mismatch:
        parts.append("Mapping unknown")
    if not parts:
        return ""
    # Self-contained note — do not cross-reference "Detailed Analysis"
    # (that section is multi-agent / diagnostic --full only).
    return ", ".join(parts)


def device_sync_label(device: str, sync_map: dict[str, str]) -> str:
    result = sync_map.get(device)
    if not result:
        return "—"
    return str(result)


def build_device_health_rows(
    topology: Any,
    fleet_sync: Any = None,
    hardware_health: Any = None,
) -> list[dict[str, Any]]:
    from nso_facts.hardware_health import device_hardware_label

    op_layers = _operational_layers(topology)
    if op_layers is None:
        return []
    static_layers = _static_layers(topology)
    phys = _merged_edges(op_layers, static_layers, "physical")
    under = _merged_edges(op_layers, static_layers, "underlay")
    route = _merged_edges(op_layers, static_layers, "routing")
    unmapped_bgp, unmapped_isis = _unmapped_by_device(topology)
    mismatch_by_edge = _mismatch_by_edge_id(topology)
    route_summaries = _route_summaries(topology)
    sync_map = build_device_sync_map(fleet_sync)
    hw = hardware_health if isinstance(hardware_health, dict) else {}

    devices = sorted(
        _devices_in_edges(phys, under, route)
        | set(unmapped_bgp)
        | set(unmapped_isis)
        | set(route_summaries)
        | set(hw)
    )
    rows: list[dict[str, Any]] = []
    for device in devices:
        bgp_up, bgp_total = _peer_up_total(device, route)
        isis_up, isis_total = _peer_up_total(device, under)
        rs = route_summaries.get(device) or {}
        routes_txt = "—"
        if rs and not rs.get("error") and rs.get("total") is not None:
            routes_txt = abbreviate_route_total(rs.get("total"))
        rows.append(
            {
                "device": device,
                "sync": device_sync_label(device, sync_map),
                "interfaces": device_interfaces_label(
                    device, phys, mismatch_by_edge
                ),
                "hardware": device_hardware_label(hw.get(device)),
                "bgp": f"{bgp_up}/{bgp_total}" if bgp_total else "—",
                "isis": f"{isis_up}/{isis_total}" if isis_total else "—",
                "routes": routes_txt,
                "notes": device_notes_label(device, phys, mismatch_by_edge),
            }
        )
    return rows


def format_device_health_table(rows: list[dict[str, Any]]) -> str:
    header = (
        f"{'Device':<16}{'Sync':<12}{'Interfaces':<20}{'Hardware':<12}"
        f"{'BGP':<6}{'IS-IS':<7}{'Routes':<10}{'Notes':<28}"
    )
    sep = "-" * len(header)
    if not rows:
        return f"{_I1}{header}\n{_I1}{sep}\n{_I1}(none)"
    lines = [f"{_I1}{header}", f"{_I1}{sep}"]
    for row in rows:
        lines.append(
            f"{_I1}{str(row['device']):<16}"
            f"{str(row.get('sync') or '—'):<12}"
            f"{str(row['interfaces']):<20}"
            f"{str(row.get('hardware') or 'Unavailable'):<12}"
            f"{str(row['bgp']):<6}"
            f"{str(row['isis']):<7}"
            f"{str(row['routes']):<10}"
            f"{str(row.get('notes') or '')}"
        )
    return "\n".join(lines)


def format_changes_bullets(delta: dict[str, Any]) -> list[str]:
    text = format_delta_section(delta)
    if text in {
        "No delta to report",
        "First run — no previous snapshot to compare.",
    }:
        if delta.get("first_run"):
            return [f"{_I1}• First run — no previous snapshot to compare"]
        return [f"{_I1}• No material changes since last report"]
    bullets: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            bullets.append(f"{_I1}• " + line[2:])
        elif line:
            bullets.append(f"{_I1}• " + line)
    return bullets or [f"{_I1}• No material changes since last report"]


def format_run_timestamp(run_id: str) -> str:
    """Turn ``2026-07-15T16:57:00Z`` into ``2026-07-15 16:57 UTC`` when possible."""
    m = re.match(
        r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2})?Z?$",
        str(run_id).strip(),
    )
    if m:
        return f"{m.group(1)} {m.group(2)} UTC"
    return str(run_id)


def format_run_provenance_lines(
    *,
    fabric_api_url: str | None = None,
    fabric_model: str | None = None,
    mcp_server_cmd: str | None = None,
    llm_skipped: bool = False,
) -> list[str]:
    """LLM provider/model and MCP server lines for the report banner."""
    lines: list[str] = []
    if llm_skipped:
        lines.append("LLM: skipped")
    else:
        provider = "FABRIC AI"
        if fabric_api_url:
            host = fabric_api_url.rstrip("/")
            for prefix in ("https://", "http://"):
                if host.startswith(prefix):
                    host = host[len(prefix) :]
                    break
            host = host.split("/")[0] or fabric_api_url
            provider = f"FABRIC AI ({host})"
        model = (fabric_model or "").strip() or "unknown"
        lines.append(f"LLM: {provider}  model={model}")
    mcp = (mcp_server_cmd or "").strip() or "unknown"
    lines.append(f"MCP: {mcp}")
    return lines


def format_report_banner(
    *,
    title: str,
    run_id: str,
    fabric_api_url: str | None = None,
    fabric_model: str | None = None,
    mcp_server_cmd: str | None = None,
    llm_skipped: bool = False,
) -> str:
    """Title + timestamp + LLM/MCP provenance between rule lines."""
    mid = [
        title,
        format_run_timestamp(run_id),
        *format_run_provenance_lines(
            fabric_api_url=fabric_api_url,
            fabric_model=fabric_model,
            mcp_server_cmd=mcp_server_cmd,
            llm_skipped=llm_skipped,
        ),
    ]
    return "\n".join(
        [
            "=====================================================",
            *mid,
            "=====================================================",
        ]
    )


def _device_list_phrase(devices: set[str] | list[str]) -> str:
    names = sorted({str(d) for d in devices if d})
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def mismatch_phrasing_examples(mismatches: list[dict[str, Any]]) -> list[str]:
    """Example human-readable inventory Action Item lines (LLM may adapt)."""
    open_issues = [
        m
        for m in mismatches
        if isinstance(m, dict) and m.get("device") and not m.get("confirmed")
    ]
    if not open_issues:
        return []

    items: list[str] = []
    n = len(open_issues)
    all_devices = {str(m["device"]) for m in open_issues}
    noun = "mapping" if n == 1 else "mappings"
    items.append(
        f"Review and confirm the {n} NSO↔device interface {noun} on "
        f"{_device_list_phrase(all_devices)}"
    )

    type_breakout = {
        str(m["device"])
        for m in open_issues
        if m.get("kind") in ("type_change", "breakout")
    }
    if type_breakout:
        items.append(
            "Resolve interface type/breakout mismatches on "
            f"{_device_list_phrase(type_breakout)}"
        )

    missing = {
        str(m["device"]) for m in open_issues if m.get("kind") == "missing"
    }
    if missing:
        items.append(
            "Confirm missing-on-box interface(s) on "
            f"{_device_list_phrase(missing)}"
        )
    return items


def _services_needing_attention(counts: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for service_type, bucket in sorted((counts or {}).items()):
        if not isinstance(bucket, dict):
            continue
        try:
            total = int(bucket.get("total", 0))
            up = int(bucket.get("up", 0))
            down = int(bucket.get("down", 0))
            degraded = int(bucket.get("degraded", 0))
            unknown = int(bucket.get("unknown", 0))
        except (TypeError, ValueError):
            continue
        if down or degraded or unknown or (total and up < total):
            out.append(
                {
                    "type": service_type,
                    "total": total,
                    "up": up,
                    "down": down,
                    "degraded": degraded,
                    "unknown": unknown,
                }
            )
    return out


def _devices_not_in_sync(fleet_sync: Any) -> list[dict[str, str]]:
    if not isinstance(fleet_sync, dict) or fleet_sync.get("status") != "success":
        return []
    data = fleet_sync.get("data") or {}
    devices = data.get("devices") or []
    out: list[dict[str, str]] = []
    for row in devices:
        if not isinstance(row, dict):
            continue
        result = str(row.get("result") or "")
        if result and result != "in-sync":
            out.append(
                {
                    "device": str(row.get("device") or ""),
                    "result": result,
                }
            )
    return [r for r in out if r["device"]]


def build_action_context(
    *,
    counts: dict[str, Any],
    fleet_sync: Any,
    mismatches: list[dict[str, Any]],
    delta: dict[str, Any],
) -> dict[str, Any]:
    """Structured facts for LLM Action Items (any scenario — not hardcoded output)."""
    open_issues = [
        m
        for m in mismatches
        if isinstance(m, dict) and m.get("device") and not m.get("confirmed")
    ]
    type_breakout = sorted(
        {
            str(m["device"])
            for m in open_issues
            if m.get("kind") in ("type_change", "breakout")
        }
    )
    missing = sorted(
        {str(m["device"]) for m in open_issues if m.get("kind") == "missing"}
    )
    return {
        "inventory": {
            "unconfirmed_mapping_count": len(open_issues),
            "devices": sorted({str(m["device"]) for m in open_issues}),
            "type_or_breakout_devices": type_breakout,
            "missing_on_box_devices": missing,
            "phrasing_examples": mismatch_phrasing_examples(mismatches),
        },
        "services_needing_attention": _services_needing_attention(counts),
        "devices_not_in_sync": _devices_not_in_sync(fleet_sync),
        "new_service_failures": (delta or {}).get("new_failures") or [],
    }


def _mismatches_from_topology(topology: Any) -> list[dict[str, Any]]:
    if not isinstance(topology, dict):
        return []
    out: list[dict[str, Any]] = []
    for issue in _mismatch_by_edge_id(topology).values():
        out.append(
            {
                "device": issue.get("device"),
                "nso": issue.get("nso"),
                "kind": issue.get("kind"),
                "confirmed": issue.get("confirmed"),
                "candidates": issue.get("candidates"),
            }
        )
    return out


def fleet_inventory_review_count(topology: Any, fleet_sync: Any = None) -> int:
    return sum(
        1
        for row in build_device_health_rows(topology, fleet_sync)
        if row.get("interfaces") == INVENTORY_REVIEW_STATUS
    )


def format_fleet_summary(
    *,
    counts: dict[str, Any],
    topology: Any,
    fleet_sync: Any = None,
    system_health: Any = None,
    hardware_health: Any = None,
    thresholds: FleetSummaryThresholds | None = None,
) -> str:
    from nso_facts.hardware_health import fleet_hardware_alert_counts

    total, in_sync, out_of_sync = fleet_device_sync_counts(fleet_sync, topology)
    operational, degraded = fleet_service_instance_counts(counts)
    bgp_up, bgp_total, isis_up, isis_total = fleet_routing_totals(topology)
    cpu_alerts, mem_alerts = fleet_infra_alert_counts(system_health, thresholds)
    temp_a, fan_a, pwr_a, cp_a = fleet_hardware_alert_counts(hardware_health)
    review = fleet_inventory_review_count(topology, fleet_sync)
    return "\n".join(
        [
            "Fleet Summary",
            "-------------",
            f"{_I1}Devices",
            f"{_I2}Total: {total}",
            f"{_I2}In Sync: {in_sync}",
            f"{_I2}Out of Sync: {out_of_sync}",
            "",
            f"{_I1}Services",
            f"{_I2}Operational: {operational}",
            f"{_I2}Degraded: {degraded}",
            "",
            f"{_I1}Routing",
            f"{_I2}BGP Peers: {bgp_up}/{bgp_total} Established",
            f"{_I2}IS-IS Adjacencies: {isis_up}/{isis_total} Up",
            "",
            f"{_I1}Infrastructure",
            f"{_I2}CPU Alerts: {cpu_alerts}",
            f"{_I2}Memory Alerts: {mem_alerts}",
            f"{_I2}Temperature Alerts: {temp_a}",
            f"{_I2}Fan Alerts: {fan_a}",
            f"{_I2}Power Supply Alerts: {pwr_a}",
            f"{_I2}Control Plane Drop Alerts: {cp_a}",
            "",
            f"{_I1}Inventory",
            f"{_I2}Devices requiring review: {review}",
        ]
    )


def _indent_section_body(block: str) -> str:
    """Keep title + underline flush; indent body lines with two spaces."""
    lines = (block or "").splitlines()
    if not lines:
        return block
    out = [lines[0]]
    i = 1
    if i < len(lines) and re.match(r"^-+$", lines[i].strip()):
        out.append(lines[i])
        i += 1
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    for line in lines[i:]:
        if not line.strip():
            out.append("")
        elif line.startswith(_I1):
            out.append(line)
        else:
            out.append(_I1 + line.lstrip())
    return "\n".join(out)


def split_overall_and_action_items(narrative: str) -> tuple[str, str]:
    """Split LLM narrative into Overall Status body and Action Items body."""
    text = (narrative or "").strip()
    if not text or text == "None reported.":
        return (
            f"Overall Status\n--------------\n{_I1}None reported.",
            f"Action Items\n------------\n{_I1}None reported.",
        )
    m = re.search(r"(?im)^Action Items\s*\n-+", text)
    if m:
        overall = text[: m.start()].rstrip()
        actions = text[m.start() :].rstrip()
        if not overall.strip():
            overall = f"Overall Status\n--------------\n{_I1}None reported."
        return _indent_section_body(overall), _indent_section_body(actions)
    # Narrative is Overall Status only
    if re.match(r"(?im)^Overall Status\b", text):
        return (
            _indent_section_body(text),
            f"Action Items\n------------\n{_I1}None reported.",
        )
    return (
        _indent_section_body(f"Overall Status\n--------------\n{text}"),
        f"Action Items\n------------\n{_I1}None reported.",
    )


def format_executive_section(
    *,
    run_id: str,
    counts: dict[str, Any],
    topology: Any,
    delta: dict[str, Any],
    llm_narrative: str,
    rich_markers: bool,
    fleet_sync: Any = None,
    system_health: Any = None,
    hardware_health: Any = None,
    thresholds: FleetSummaryThresholds | None = None,
    operational_assessment: str = "None reported.",
    fabric_api_url: str | None = None,
    fabric_model: str | None = None,
    mcp_server_cmd: str | None = None,
    llm_skipped: bool = False,
    title: str = "NSO Operations Snapshot",
) -> str:
    """Full Executive Summary body (no outer report title from assemble)."""
    narrative = (llm_narrative or "").strip() or "None reported."
    if not rich_markers:
        narrative = replace_markers_for_plain(narrative)

    overall, actions = split_overall_and_action_items(narrative)
    fleet_block = format_fleet_summary(
        counts=counts if isinstance(counts, dict) else {},
        topology=topology,
        fleet_sync=fleet_sync,
        system_health=system_health,
        hardware_health=hardware_health,
        thresholds=thresholds,
    )

    banner = format_report_banner(
        title=title,
        run_id=run_id,
        fabric_api_url=fabric_api_url,
        fabric_model=fabric_model,
        mcp_server_cmd=mcp_server_cmd,
        llm_skipped=llm_skipped,
    )

    assessment = (operational_assessment or "").strip() or "None reported."
    if not rich_markers:
        assessment = replace_markers_for_plain(assessment)
    assessment_lines = [
        f"{_I1}{line}" if line.strip() and not line.startswith(_I1) else line
        for line in assessment.splitlines()
    ] or [f"{_I1}None reported."]

    parts = [
        banner,
        "",
        overall,
        "",
        fleet_block,
        "",
        actions,
        "",
        "Changes Since Last Report",
        "-------------------------",
        *format_changes_bullets(delta),
        "",
        "Service Summary",
        "---------------",
        *format_service_summary_lines(counts),
        "",
        "Device Health",
        "-------------",
        format_device_health_table(
            build_device_health_rows(
                topology, fleet_sync, hardware_health=hardware_health
            )
        ),
        "",
        "Operational Assessment",
        "----------------------",
        *assessment_lines,
    ]
    return "\n".join(parts)


def executive_fact_pack(
    snapshot: dict[str, Any],
    delta: dict[str, Any],
) -> dict[str, Any]:
    topology = snapshot.get("topology")
    fleet_sync = snapshot.get("fleet_sync")
    rows = build_device_health_rows(
        topology,
        fleet_sync,
        hardware_health=snapshot.get("hardware_health") or {},
    )
    mismatches = _mismatches_from_topology(topology)
    counts = snapshot.get("counts") or {}
    health = snapshot.get("system_health") or {}
    return {
        "run_id": snapshot.get("run_id"),
        "counts": counts,
        "fleet_sync": fleet_sync,
        "system_health": health,
        "device_health_rows": rows,
        "interface_mismatches": mismatches,
        "action_context": build_action_context(
            counts=counts if isinstance(counts, dict) else {},
            fleet_sync=fleet_sync,
            mismatches=mismatches,
            delta=delta if isinstance(delta, dict) else {},
        ),
        "delta": {
            "first_run": delta.get("first_run"),
            "new_failures": delta.get("new_failures"),
            "recoveries": delta.get("recoveries"),
            "status_changes": delta.get("status_changes"),
            "removed": delta.get("removed"),
            "counts": delta.get("counts"),
            "has_changes": delta_has_changes(delta),
        },
        "interface_troubleshooting": (
            ((topology or {}).get("operational") or {}).get(
                "interface_troubleshooting"
            )
            if isinstance(topology, dict)
            else None
        ),
    }
