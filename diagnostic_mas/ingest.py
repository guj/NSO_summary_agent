"""Convert spine collector outputs into Evidence + Issues."""

from __future__ import annotations

from typing import Any

from diagnostic_mas.case import CaseFile, add_evidence, open_issue


def ingest_layer_spine(
    case: CaseFile,
    *,
    layer: str,
    role: str,
    static_summary: dict[str, Any],
    operational_summary: dict[str, Any],
    issues: list[dict[str, Any]],
    static_edges: list[dict[str, Any]] | None = None,
    operational_edges: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "static_summary": static_summary,
        "operational_summary": operational_summary,
    }
    if static_edges is not None:
        payload["static_edges"] = static_edges
    if operational_edges is not None:
        payload["operational_edges"] = operational_edges
    if extra:
        payload["extra"] = extra

    eid = add_evidence(
        case,
        {
            "kind": "spine",
            "role": role,
            "layer": layer,
            "payload": payload,
        },
    )

    for issue in issues:
        devices = issue.get("devices")
        kwargs: dict[str, Any] = {
            "code": str(issue.get("code") or "unknown"),
            "message": str(issue.get("message") or ""),
            "evidence_ids": [eid],
            "severity": str(issue.get("severity") or "medium"),
            "layer": str(issue.get("layer") or layer),
            "edge_id": issue.get("edge_id"),
            "devices": list(devices) if isinstance(devices, list) else None,
        }
        if isinstance(issue.get("live_l2"), dict):
            kwargs["live_l2"] = issue["live_l2"]
        if isinstance(issue.get("device_sync"), dict):
            kwargs["device_sync"] = issue["device_sync"]
        if "in_sync" in issue:
            kwargs["in_sync"] = issue.get("in_sync")
        if issue.get("system_status"):
            kwargs["system_status"] = issue["system_status"]
        if issue.get("dataplane_status"):
            kwargs["dataplane_status"] = issue["dataplane_status"]
        open_issue(case, **kwargs)


def ingest_quarantined_devices(case: CaseFile) -> list[str]:
    """Open one issue per device quarantined for live MCP failure this run.

    Idempotent for already-recorded ``device_live_unreachable`` codes on the
    same device. Returns newly opened issue ids.

    Timeout-only NSO API failures are worded as ``live query timed out`` —
    not device unreachability — unless the reason strongly indicates
    unreachability.
    """
    from nso_facts.mcp_client import (
        format_live_mcp_quarantine_prose,
        quarantined_devices,
    )

    already = {
        str(d)
        for issue in case.issues
        if isinstance(issue, dict)
        and issue.get("code") == "device_live_unreachable"
        for d in (issue.get("devices") or [])
    }
    opened: list[str] = []
    for device, reason in sorted(quarantined_devices().items()):
        if device in already:
            continue
        iid = open_issue(
            case,
            code="device_live_unreachable",
            message=format_live_mcp_quarantine_prose(device, reason, for_issue=True),
            evidence_ids=[],
            severity="high",
            layer="device",
            devices=[device],
        )
        opened.append(iid)
    return opened
