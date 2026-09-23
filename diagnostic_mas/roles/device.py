"""Device role: box-local follow-ups from handoffs."""

from __future__ import annotations

from typing import Any

from diagnostic_mas.case import CaseFile, add_evidence, set_issue_status
from diagnostic_mas.deep_checks import any_probe_succeeded, run_role_deep_checks
from nso_facts.mcp_client import call_mcp


async def run_device_followup(
    client: Any,
    case: CaseFile,
    handoff: dict[str, Any],
) -> None:
    devices = list(handoff.get("devices") or [])
    actions = list(handoff.get("suggested_actions") or ["get_hardware_health"])
    issue_ids = list(handoff.get("issue_ids") or [])
    payloads: list[dict[str, Any]] = []
    ok_any = False

    # Prefer gated multi-check plan when device names are known
    plan: list[dict[str, Any]] = []
    for device in devices:
        for action in actions:
            if action == "exec_show":
                plan.append(
                    {
                        "check": "exec_show",
                        "args": {"device": device, "command": "platform"},
                        "reason": handoff.get("reason"),
                        "issue_id": (issue_ids[0] if issue_ids else None),
                    }
                )
            elif action in ("get_hardware_health", "get_interface_health"):
                plan.append(
                    {
                        "check": action,
                        "args": {"device_name": device},
                        "reason": handoff.get("reason"),
                        "issue_id": (issue_ids[0] if issue_ids else None),
                    }
                )

    if plan:
        try:
            results = await run_role_deep_checks(client, "device", plan)
            payloads.extend(results)
            ok_any = any_probe_succeeded(results)
        except Exception as exc:  # noqa: BLE001
            payloads.append({"error": str(exc), "plan": plan})
    else:
        for device in devices:
            try:
                result = await call_mcp(
                    client, "get_hardware_health", {"device_name": device}
                )
                payloads.append(
                    {"device": device, "check": "get_hardware_health", "result": result}
                )
                ok_any = True
            except Exception as exc:  # noqa: BLE001
                payloads.append(
                    {"device": device, "check": "get_hardware_health", "error": str(exc)}
                )

    add_evidence(
        case,
        {
            "kind": "device_followup",
            "role": "device",
            "layer": "device",
            "payload": {"handoff_id": handoff.get("id"), "results": payloads},
        },
    )

    status = "explained" if ok_any else "needs_human"
    for iid in issue_ids:
        try:
            set_issue_status(case, str(iid), status)
        except KeyError:
            continue


async def run_device_health_spine(
    client: Any,
    settings: Any,
    device_names: list[str],
) -> dict[str, Any]:
    """Lean device focus: fleet sync + HW/system for named devices only.

    Skips service-type listing, get_services, and physical operational probes.
    """
    from nso_facts.fact_pack import build_fact_pack

    pack = await build_fact_pack(
        client,
        settings,
        device_names=device_names,
        include_capabilities=False,
        include_system_health=True,
        include_hardware_health=True,
        include_fleet_sync=True,
        include_physical_operational=False,
        only_service_types=[],  # no service MCP
    )
    return {
        "static_summary": {"devices": len(device_names)},
        "operational_summary": {},
        "issues": [],
        "static_edges": [],
        "operational_edges": [],
        "extra": {
            "services": {},
            "fleet_sync": pack.get("fleet_sync"),
            "hardware_health": pack.get("hardware_health") or {},
            "system_health": pack.get("system_health") or {},
            "physical_operational_edges": [],
            "physical_issues": [],
            "counts": {},
            "lean_device": True,
        },
    }
