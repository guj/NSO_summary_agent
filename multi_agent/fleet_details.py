"""Explain Device Health Inventory/Hardware Review in multi-agent reports."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from nso_facts.hardware_health import device_hardware_label

_I1 = "  "
_I2 = "    "


def _physical_issues(fleet_pack: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(fleet_pack, dict):
        return []
    topo = fleet_pack.get("topology")
    if isinstance(topo, dict):
        op = topo.get("operational") or {}
        if isinstance(op, dict):
            issues = op.get("issues") or []
            if isinstance(issues, list) and issues:
                return [i for i in issues if isinstance(i, dict)]
    raw = fleet_pack.get("physical_issues")
    if isinstance(raw, list):
        return [i for i in raw if isinstance(i, dict)]
    return []


def _device_from_message(message: str) -> str | None:
    parts = str(message or "").split(None, 1)
    return parts[0] if parts else None


def inventory_issues_by_device(
    fleet_pack: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Group physical inventory issues that drive Interfaces=Inventory Review."""
    by_dev: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for issue in _physical_issues(fleet_pack):
        code = issue.get("code")
        if code not in {"config_live_mismatch", "unexpected_live_object"}:
            # still include other physical layer codes if present
            if issue.get("layer") != "physical":
                continue
            if code in {
                "unknown_neighbor_address",
                "unknown_neighbor_system_id",
            }:
                continue
        msg = str(issue.get("message") or "")
        device = _device_from_message(msg)
        if not device:
            continue
        if code not in {"config_live_mismatch", "unexpected_live_object"}:
            continue
        by_dev[device].append(issue)
    return dict(by_dev)


def hardware_review_reasons(
    hardware_health: Any,
) -> dict[str, list[str]]:
    """Per-device reasons when Hardware column would be Review."""
    if not isinstance(hardware_health, dict):
        return {}
    out: dict[str, list[str]] = {}
    for device, entry in hardware_health.items():
        if device_hardware_label(entry if isinstance(entry, dict) else None) != "Review":
            continue
        if not isinstance(entry, dict):
            continue
        reasons: list[str] = []
        for key, label in (
            ("temperature", "temperature alert"),
            ("fans", "fan alert"),
            ("power", "power-supply alert"),
        ):
            rows = entry.get(key)
            if isinstance(rows, list) and any(
                isinstance(x, dict) and x.get("ok") is False for x in rows
            ):
                reasons.append(label)
        drops = 0
        cps = entry.get("control_plane")
        if isinstance(cps, list):
            for row in cps:
                if isinstance(row, dict):
                    try:
                        drops += int(row.get("dropped") or 0)
                    except (TypeError, ValueError):
                        pass
        if drops > 0:
            reasons.append(f"control-plane drops={drops}")
        if reasons:
            out[str(device)] = reasons
    return out


def build_fleet_followup_actions(
    fleet_pack: dict[str, Any] | None,
    *,
    limit: int = 6,
) -> list[str]:
    """Compact Action Items for inventory/hardware (not ISIS/BGP)."""
    items: list[str] = []
    inv = inventory_issues_by_device(fleet_pack)
    for device in sorted(inv):
        mismatches = [
            i for i in inv[device] if i.get("code") == "config_live_mismatch"
        ]
        unexpected = [
            i for i in inv[device] if i.get("code") == "unexpected_live_object"
        ]
        bits: list[str] = []
        if mismatches:
            bits.append(f"{len(mismatches)} NSO↔live interface mismatch(es)")
        if unexpected:
            bits.append(f"{len(unexpected)} unexpected live interface(s)")
        if bits:
            items.append(
                f"{device}: {', '.join(bits)} (inventory) — see Detailed Analysis"
            )
    hw = hardware_review_reasons(
        (fleet_pack or {}).get("hardware_health") if fleet_pack else None
    )
    for device in sorted(hw):
        items.append(
            f"{device}: hardware Review ({', '.join(hw[device])}) — see Detailed Analysis"
        )
    return items[:limit]


def format_inventory_hardware_section(
    fleet_pack: dict[str, Any] | None,
    *,
    max_per_device: int = 12,
) -> str:
    """Detailed Analysis section explaining Inventory/Hardware Review columns."""
    inv = inventory_issues_by_device(fleet_pack)
    hw = hardware_review_reasons(
        (fleet_pack or {}).get("hardware_health") if fleet_pack else None
    )
    if not inv and not hw:
        return ""

    lines = [
        "## Inventory / hardware",
        "",
        "Explains Device Health columns Interfaces=Inventory Review and Hardware=Review.",
        "(ISIS/BGP unmapped neighbors are listed under Action Items / domain sections.)",
        "",
    ]
    for device in sorted(set(inv) | set(hw)):
        lines.append(f"### {device}")
        lines.append("")
        if device in inv:
            mismatches = [
                i for i in inv[device] if i.get("code") == "config_live_mismatch"
            ]
            unexpected = [
                i for i in inv[device] if i.get("code") == "unexpected_live_object"
            ]
            if mismatches:
                lines.append(f"{_I1}NSO↔live interface mismatches ({len(mismatches)}):")
                for issue in mismatches[:max_per_device]:
                    msg = str(issue.get("message") or "")
                    parts = msg.split(None, 1)
                    rest = parts[1] if len(parts) > 1 else msg
                    lines.append(f"{_I2}- {rest}")
                if len(mismatches) > max_per_device:
                    lines.append(
                        f"{_I2}- … {len(mismatches) - max_per_device} more"
                    )
            if unexpected:
                lines.append(
                    f"{_I1}Unexpected live interfaces not in static ({len(unexpected)}):"
                )
                shown = 0
                for issue in unexpected:
                    if shown >= max_per_device:
                        break
                    msg = str(issue.get("message") or "")
                    parts = msg.split(None, 1)
                    rest = parts[1] if len(parts) > 1 else msg
                    # skip noisy always-present mgmt/null if many — still show
                    lines.append(f"{_I2}- {rest}")
                    shown += 1
                if len(unexpected) > max_per_device:
                    lines.append(
                        f"{_I2}- … {len(unexpected) - max_per_device} more"
                    )
        if device in hw:
            lines.append(f"{_I1}Hardware Review reasons:")
            for reason in hw[device]:
                lines.append(f"{_I2}- {reason}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
