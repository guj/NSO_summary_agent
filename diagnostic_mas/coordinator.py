"""Coordinator: mandatory spines + autonomous loop."""

from __future__ import annotations

from typing import Any

from agent.config import Settings
from diagnostic_mas.case import (
    CaseFile,
    add_evidence,
    debit_deep_check,
    set_issue_status,
    should_stop,
)
from diagnostic_mas.deep_checks import any_probe_succeeded, run_role_deep_checks
from diagnostic_mas.handoff import apply_handoff
from diagnostic_mas.ingest import ingest_layer_spine, ingest_quarantined_devices
from diagnostic_mas.planner import plan_turn_for_issue
from diagnostic_mas.roles.bgp import run_bgp_spine
from diagnostic_mas.roles.device import run_device_followup, run_device_health_spine
from diagnostic_mas.roles.isis import run_isis_spine
from diagnostic_mas.roles.service import run_service_spine
from nso_facts.topology.physical import collect_static_physical

_DEVICE_ACTIONS = frozenset(
    {"get_hardware_health", "get_interface_health", "exec_show"}
)


async def run_mandatory_spines(
    client: Any,
    settings: Settings,
    case: CaseFile,
    device_names: list[str],
    *,
    run_isis: bool = True,
    run_bgp: bool = True,
    run_service: bool = True,
    run_device: bool = False,
    physical_edges: list[dict[str, Any]] | None = None,
    service_type: str | None = None,
    service_id: str | None = None,
    filter_services_to_devices: bool = False,
) -> list[dict[str, Any]]:
    """Collect physical + enabled layer spines into the case. Returns physical_edges."""
    # Lean focus: skip physical inventory walk
    lean_service = (
        run_service
        and not run_isis
        and not run_bgp
        and bool(service_type or service_id)
    )
    lean_device = run_device and not run_isis and not run_bgp and not run_service
    if physical_edges is None:
        if lean_service or lean_device:
            physical_edges = []
        else:
            physical_edges, _, _ = await collect_static_physical(
                client, device_names
            )

    case.device_names = list(device_names)
    ingest_layer_spine(
        case,
        layer="physical",
        role="physical",
        static_summary={"edge_count": len(physical_edges)},
        operational_summary={},
        issues=[],
        static_edges=physical_edges,
        operational_edges=[],
    )

    if run_isis:
        spine = await run_isis_spine(client, device_names, physical_edges)
        ingest_layer_spine(
            case,
            layer="underlay",
            role="isis",
            static_summary=spine.get("static_summary") or {},
            operational_summary=spine.get("operational_summary") or {},
            issues=list(spine.get("issues") or []),
            static_edges=spine.get("static_edges"),
            operational_edges=spine.get("operational_edges"),
            extra={"coverage": spine.get("coverage")},
        )

    if run_bgp:
        spine = await run_bgp_spine(client, device_names)
        ingest_layer_spine(
            case,
            layer="routing",
            role="bgp",
            static_summary=spine.get("static_summary") or {},
            operational_summary=spine.get("operational_summary") or {},
            issues=list(spine.get("issues") or []),
            static_edges=spine.get("static_edges"),
            operational_edges=spine.get("operational_edges"),
            extra={"coverage": spine.get("coverage")},
        )

    if run_service:
        spine = await run_service_spine(
            client,
            settings,
            device_names,
            physical_edges,
            service_type=service_type,
            service_id=service_id,
            filter_to_devices=filter_services_to_devices,
        )
        ingest_layer_spine(
            case,
            layer="services",
            role="service",
            static_summary=spine.get("static_summary") or {},
            operational_summary=spine.get("operational_summary") or {},
            issues=list(spine.get("issues") or []),
            static_edges=spine.get("static_edges"),
            operational_edges=spine.get("operational_edges"),
            extra=spine.get("extra"),
        )

    if run_device:
        spine = await run_device_health_spine(client, settings, device_names)
        ingest_layer_spine(
            case,
            layer="services",
            role="service",
            static_summary=spine.get("static_summary") or {},
            operational_summary=spine.get("operational_summary") or {},
            issues=list(spine.get("issues") or []),
            static_edges=[],
            operational_edges=[],
            extra=spine.get("extra"),
        )

    ingest_quarantined_devices(case)
    return physical_edges


def _oldest_open_issue(case: CaseFile) -> dict[str, Any] | None:
    from diagnostic_mas.dataplane_verify import dataplane_diagnosed_names

    diagnosed = dataplane_diagnosed_names(case)
    for issue in case.issues:
        if issue.get("status") != "open":
            continue
        # Dataplane verify already concluded this service — do not re-burn budget.
        edge = issue.get("edge_id")
        if (
            issue.get("layer") == "services"
            and isinstance(edge, str)
            and edge in diagnosed
        ):
            set_issue_status(case, issue["id"], "explained")
            continue
        return issue
    return None


def _devices_from_issue(issue: dict[str, Any]) -> list[str]:
    devices = issue.get("devices")
    if isinstance(devices, list) and devices:
        return [str(d) for d in devices if d]
    message = str(issue.get("message") or "")
    edge = str(issue.get("edge_id") or "")
    found: list[str] = []
    for token in message.replace("↔", " ").replace("<->", " ").replace("|", " ").split():
        if "-data-" in token or token.endswith("-sw") or token.endswith("-rr"):
            found.append(token.strip(".,:;"))
    if not found and edge:
        for part in edge.replace("|", " ").split():
            if part and part not in found:
                found.append(part)
    return found


def _heuristic_handoff(issue: dict[str, Any]) -> dict[str, Any] | None:
    layer = str(issue.get("layer") or "")
    devices = _devices_from_issue(issue)
    if layer in {"underlay", "routing", "services"} and devices:
        return {
            "from_role": {
                "underlay": "isis",
                "routing": "bgp",
                "services": "service",
            }.get(layer, "isis"),
            "to_role": "device",
            "issue_ids": [issue["id"]],
            "reason": f"heuristic escalate {layer} issue to device",
            "suggested_actions": ["get_hardware_health"],
            "budget_cost": 1,
            "devices": devices[:2],
        }
    return None


async def _apply_heuristic_device(
    client: Any, case: CaseFile, issue: dict[str, Any]
) -> None:
    handoff = _heuristic_handoff(issue)
    if handoff is None:
        set_issue_status(case, issue["id"], "needs_human")
        return
    ok, _err = apply_handoff(case, handoff, _DEVICE_ACTIONS)
    if not ok:
        set_issue_status(case, issue["id"], "needs_human")
        return
    await run_device_followup(client, case, handoff)


def _record_hypotheses(
    case: CaseFile, hypotheses: list[dict[str, Any]], issue_id: str
) -> None:
    for hy in hypotheses:
        text = str(hy.get("text") or "").strip()
        if not text:
            continue
        case.hypotheses.append(
            {
                "id": f"hy_{len(case.hypotheses) + 1}",
                "text": text,
                "issue_ids": list(hy.get("issue_ids") or [issue_id]),
            }
        )


async def _apply_llm_turn(
    client: Any,
    settings: Settings,
    case: CaseFile,
    issue: dict[str, Any],
    *,
    device_names: set[str],
    llm_turn_fn: Any | None = None,
) -> None:
    """Plan + optional handoffs + hypotheses; Evidence only from MCP."""
    if (
        case.budget.deep_checks_used >= case.budget.max_deep_checks
        and case.budget.handoffs_used >= case.budget.max_handoffs
    ):
        set_issue_status(case, issue["id"], "budget_exhausted")
        return

    role, turn = plan_turn_for_issue(
        settings,
        case,
        issue,
        device_names=device_names,
        llm_turn_fn=llm_turn_fn,
    )
    if role is None:
        await _apply_heuristic_device(client, case, issue)
        return

    _record_hypotheses(case, list(turn.get("hypotheses") or []), issue["id"])

    gated = list(turn.get("plans") or [])
    remaining = case.budget.max_deep_checks - case.budget.deep_checks_used
    gated = gated[: max(0, remaining)]

    ran_checks = False
    if gated:
        case.plans.extend(gated)
        results = await run_role_deep_checks(client, role, gated)
        for _ in gated:
            if not debit_deep_check(case, 1):
                break
        add_evidence(
            case,
            {
                "kind": "deep_check",
                "role": role,
                "layer": issue.get("layer"),
                "payload": {
                    "issue_id": issue.get("id"),
                    "plan": gated,
                    "results": results,
                },
            },
        )
        ran_checks = any_probe_succeeded(results)

    handoff_ok = False
    for raw_ho in list(turn.get("handoffs") or []):
        if case.budget.handoffs_used >= case.budget.max_handoffs:
            break
        ho = dict(raw_ho)
        ho.setdefault("from_role", role)
        ho.setdefault("to_role", "device")
        # Force issue linkage to the current open issue when missing/wrong
        ids = list(ho.get("issue_ids") or [])
        if issue["id"] not in ids:
            ho["issue_ids"] = [issue["id"]]
        devices = list(ho.get("devices") or []) or _devices_from_issue(issue)
        # Drop invented devices
        devices = [d for d in devices if d in device_names] or _devices_from_issue(
            issue
        )
        devices = [d for d in devices if d in device_names]
        ho["devices"] = devices[:2]
        ho.setdefault("suggested_actions", ["get_hardware_health"])
        ho.setdefault("budget_cost", 1)
        ho.setdefault("reason", "llm handoff")
        if not devices:
            continue
        ok, _err = apply_handoff(case, ho, _DEVICE_ACTIONS)
        if not ok:
            continue
        await run_device_followup(client, case, case.handoffs[-1])
        handoff_ok = True
        break  # one handoff per issue turn in v1

    if ran_checks or handoff_ok:
        # Device followup may already have set status; keep explained if still open
        if issue.get("status") == "open":
            set_issue_status(case, issue["id"], "explained")
        return

    # Nothing useful from LLM — heuristic Device escalate, else needs_human
    await _apply_heuristic_device(client, case, issue)


async def run_autonomous_loop(
    client: Any,
    settings: Settings,
    case: CaseFile,
    *,
    device_names: list[str] | None = None,
    llm_turn_fn: Any | None = None,
    llm_plan_fn: Any | None = None,
) -> None:
    """Budgeted LLM deep-checks / handoffs until stop.

    Call only when an LLM model is configured/enabled.
    """
    import sys

    names = set(device_names or [])
    print(
        f"[autonomous] start deep_checks≤{case.budget.max_deep_checks} "
        f"handoffs≤{case.budget.max_handoffs}",
        file=sys.stderr,
    )

    # Back-compat: older tests inject llm_plan_fn returning a plan list
    turn_fn = llm_turn_fn
    if turn_fn is None and llm_plan_fn is not None:

        def turn_fn(settings: Settings, **kwargs: Any) -> dict[str, Any]:
            plans = llm_plan_fn(settings, **kwargs)
            if isinstance(plans, list):
                return {"plans": plans, "handoffs": [], "hypotheses": []}
            if isinstance(plans, dict):
                return {
                    "plans": list(plans.get("plans") or []),
                    "handoffs": list(plans.get("handoffs") or []),
                    "hypotheses": list(plans.get("hypotheses") or []),
                }
            return {"plans": [], "handoffs": [], "hypotheses": []}

    guard = 0
    while not should_stop(case) and guard < 100:
        guard += 1
        issue = _oldest_open_issue(case)
        if issue is None:
            break

        if (
            case.budget.deep_checks_used >= case.budget.max_deep_checks
            and case.budget.handoffs_used >= case.budget.max_handoffs
        ):
            set_issue_status(case, issue["id"], "budget_exhausted")
            continue

        await _apply_llm_turn(
            client,
            settings,
            case,
            issue,
            device_names=names,
            llm_turn_fn=turn_fn,
        )

    for issue in case.issues:
        if issue.get("status") == "open":
            if (
                case.budget.deep_checks_used >= case.budget.max_deep_checks
                and case.budget.handoffs_used >= case.budget.max_handoffs
            ):
                set_issue_status(case, issue["id"], "budget_exhausted")
