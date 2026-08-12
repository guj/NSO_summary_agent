"""Slice ISIS/BGP topology + issues for one device."""

from __future__ import annotations

from typing import Any

from multi_agent.base import AgentResult


def edge_touches_device(edge: dict[str, Any], device: str) -> bool:
    local = edge.get("local") or {}
    remote = edge.get("remote") or {}
    if local.get("device") == device or remote.get("device") == device:
        return True
    eid = str(edge.get("id") or "")
    # isis:devA:iface:devB:iface  or bgp:…
    parts = eid.split(":")
    if len(parts) >= 2 and parts[0] in ("isis", "bgp", "svc", "if"):
        if device in parts:
            return True
    return False


def issue_touches_device(issue: dict[str, Any], device: str) -> bool:
    eid = issue.get("edge_id")
    if isinstance(eid, str) and device in eid.split(":"):
        return True
    msg = str(issue.get("message") or "")
    if device in msg:
        return True
    return False


def slice_topology_for_device(
    device: str,
    isis: AgentResult,
    bgp: AgentResult,
) -> dict[str, Any]:
    """Seed pack: BGP/ISIS edges + issues for this device (from earlier spines)."""
    isis_op = [e for e in isis.operational_edges if edge_touches_device(e, device)]
    isis_st = [e for e in isis.static_edges if edge_touches_device(e, device)]
    bgp_op = [e for e in bgp.operational_edges if edge_touches_device(e, device)]
    bgp_st = [e for e in bgp.static_edges if edge_touches_device(e, device)]
    isis_issues = [i for i in isis.issues if issue_touches_device(i, device)]
    bgp_issues = [i for i in bgp.issues if issue_touches_device(i, device)]
    return {
        "device": device,
        "isis": {
            "static_edges": isis_st,
            "operational_edges": isis_op,
            "issues": isis_issues,
        },
        "bgp": {
            "static_edges": bgp_st,
            "operational_edges": bgp_op,
            "issues": bgp_issues,
        },
    }


def devices_with_topology_issues(
    isis: AgentResult,
    bgp: AgentResult,
    inventory: list[str],
) -> list[str]:
    """Devices that appear on issue edge_ids (inventory-ordered).

    Does not substring-match messages (avoids false hits like ``c`` in
    ``unidirectional``).
    """
    hit: set[str] = set()
    inv = set(inventory)
    for issue in isis.issues + bgp.issues:
        if not isinstance(issue, dict):
            continue
        eid = issue.get("edge_id")
        if isinstance(eid, str):
            for part in eid.split(":"):
                if part in inv:
                    hit.add(part)
        for edge in isis.static_edges + isis.operational_edges + bgp.static_edges + bgp.operational_edges:
            if not isinstance(edge, dict):
                continue
            if edge.get("id") != eid:
                continue
            for side in (edge.get("local"), edge.get("remote")):
                if isinstance(side, dict):
                    dev = side.get("device")
                    if isinstance(dev, str) and dev in inv:
                        hit.add(dev)
    return [d for d in inventory if d in hit]
