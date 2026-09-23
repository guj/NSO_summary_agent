"""LLM planner for open Issues: plans + handoffs + hypotheses (never Evidence)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agent.config import Settings
from diagnostic_mas.case import CaseFile
from diagnostic_mas.deep_checks import ROLE_ALLOWLISTS, SERVICE_ALLOWLIST
from multi_agent.base import (
    BGP_ALLOWLIST,
    DEVICE_ALLOWLIST,
    ISIS_ALLOWLIST,
    gate_plan,
    parse_plan_json,
)

_PROMPTS = Path(__file__).resolve().parent / "prompts"
_MULTI_PROMPTS = Path(__file__).resolve().parent.parent / "multi_agent" / "prompts"

_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")

_ROLE_FOR_LAYER = {
    "underlay": "isis",
    "routing": "bgp",
    "services": "service",
    "device": "device",
}

_ALLOWLIST_FOR_ROLE = {
    "isis": ISIS_ALLOWLIST,
    "bgp": BGP_ALLOWLIST,
    "service": SERVICE_ALLOWLIST,
    "device": DEVICE_ALLOWLIST,
}

_PROMPT_FOR_ROLE = {
    "isis": "layer_plan.txt",
    "bgp": "layer_plan.txt",
    "service": "service_plan.txt",
    "device": "device_plan.txt",
}


def role_for_issue_layer(layer: str | None) -> str | None:
    return _ROLE_FOR_LAYER.get(str(layer or ""))


def _spine_context(case: CaseFile, layer: str) -> dict[str, Any]:
    for ev in reversed(case.evidence):
        if ev.get("kind") == "spine" and ev.get("layer") == layer:
            payload = ev.get("payload") or {}
            return {
                "operational_summary": payload.get("operational_summary") or {},
                "static_summary": payload.get("static_summary") or {},
            }
    return {"operational_summary": {}, "static_summary": {}}


def _system_prompt(role: str) -> str:
    name = _PROMPT_FOR_ROLE.get(role)
    if name:
        path = _PROMPTS / name
        if path.is_file():
            return path.read_text(encoding="utf-8")
        # Fall back to multi_agent prompts for isis/bgp if needed
        alt = _MULTI_PROMPTS / (
            "isis_plan.txt" if role == "isis" else "bgp_plan.txt"
        )
        if alt.is_file():
            return alt.read_text(encoding="utf-8")
    return (
        "Propose JSON with keys plans, handoffs, hypotheses for the issue. "
        "Do not invent device names. Empty arrays are fine."
    )


def parse_planner_response(text: str) -> dict[str, Any]:
    """Parse model output into {plans, handoffs, hypotheses}.

    Accepts a bare JSON array (legacy = plans only) or an object.
    """
    raw = (text or "").strip()
    empty = {"plans": [], "handoffs": [], "hypotheses": []}
    if not raw:
        return empty
    data: Any = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        obj = _JSON_OBJECT_RE.search(raw)
        if obj:
            try:
                data = json.loads(obj.group(0))
            except json.JSONDecodeError:
                data = None
        if data is None:
            plans = parse_plan_json(raw)
            return {"plans": plans, "handoffs": [], "hypotheses": []}

    if isinstance(data, list):
        return {
            "plans": [x for x in data if isinstance(x, dict)],
            "handoffs": [],
            "hypotheses": [],
        }
    if not isinstance(data, dict):
        return empty
    plans = data.get("plans")
    if not isinstance(plans, list):
        plans = []
    handoffs = data.get("handoffs")
    if not isinstance(handoffs, list):
        handoffs = []
    hypotheses = data.get("hypotheses")
    if not isinstance(hypotheses, list):
        hypotheses = []
    return {
        "plans": [x for x in plans if isinstance(x, dict)],
        "handoffs": [x for x in handoffs if isinstance(x, dict)],
        "hypotheses": [x for x in hypotheses if isinstance(x, dict)],
    }


def llm_turn_raw(
    settings: Settings,
    *,
    role: str,
    issue: dict[str, Any],
    case: CaseFile,
    allowlist: frozenset[str],
) -> dict[str, Any]:
    """Call LLM; return ungated {plans, handoffs, hypotheses}."""
    import sys
    import time

    from agent.summarize import FABRIC_CHAT_TIMEOUT_SEC, fabric_openai_client

    layer = str(issue.get("layer") or "")
    ctx = _spine_context(case, layer)
    issue_payload: dict[str, Any] = {
        "id": issue.get("id"),
        "code": issue.get("code"),
        "message": issue.get("message"),
        "edge_id": issue.get("edge_id"),
        "layer": layer,
        "devices": issue.get("devices"),
    }
    if isinstance(issue.get("live_l2"), dict):
        issue_payload["live_l2"] = issue["live_l2"]
    if "in_sync" in issue:
        issue_payload["in_sync"] = issue.get("in_sync")
    if isinstance(issue.get("device_sync"), dict):
        issue_payload["device_sync"] = issue["device_sync"]
    user = json.dumps(
        {
            "issue": issue_payload,
            "operational_summary": ctx.get("operational_summary"),
            "static_summary": ctx.get("static_summary"),
            "allowlist": sorted(allowlist),
            "allowed_handoff_targets": ["device"],
        },
        default=str,
    )
    edge = issue.get("edge_id") or issue.get("id") or "?"
    t0 = time.monotonic()
    print(
        f"[autonomous] LLM plan start role={role} issue={edge} "
        f"timeout={FABRIC_CHAT_TIMEOUT_SEC}s",
        file=sys.stderr,
    )
    try:
        client = fabric_openai_client(settings, timeout=FABRIC_CHAT_TIMEOUT_SEC)
        resp = client.chat.completions.create(
            model=settings.fabric_model,
            messages=[
                {"role": "system", "content": _system_prompt(role)},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - t0
        print(
            f"[autonomous] LLM plan failed after {elapsed:.1f}s "
            f"role={role} issue={edge}: {exc}",
            file=sys.stderr,
        )
        return {"plans": [], "handoffs": [], "hypotheses": []}
    elapsed = time.monotonic() - t0
    print(
        f"[autonomous] LLM plan done in {elapsed:.1f}s role={role} issue={edge}",
        file=sys.stderr,
    )
    text = (resp.choices[0].message.content or "") if resp.choices else ""
    return parse_planner_response(text)


def plan_turn_for_issue(
    settings: Settings,
    case: CaseFile,
    issue: dict[str, Any],
    *,
    device_names: set[str],
    llm_turn_fn: Any | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Return (role, {plans gated, handoffs raw, hypotheses})."""
    role = role_for_issue_layer(issue.get("layer"))
    if role is None:
        return None, {"plans": [], "handoffs": [], "hypotheses": []}
    allowlist = _ALLOWLIST_FOR_ROLE.get(role) or ROLE_ALLOWLISTS.get(role) or frozenset()
    turn_fn = llm_turn_fn or llm_turn_raw
    raw = turn_fn(
        settings,
        role=role,
        issue=issue,
        case=case,
        allowlist=allowlist,
    )
    gated = gate_plan(
        list(raw.get("plans") or []),
        allowlist=allowlist,
        device_names=device_names,
    )
    return role, {
        "plans": gated,
        "handoffs": list(raw.get("handoffs") or []),
        "hypotheses": list(raw.get("hypotheses") or []),
    }


# Back-compat for older tests / callers
def plan_and_gate_for_issue(
    settings: Settings,
    case: CaseFile,
    issue: dict[str, Any],
    *,
    device_names: set[str],
    llm_plan_fn: Any | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Return (role, gated plan list). Wraps plan_turn_for_issue."""

    def _wrap(settings: Settings, **kwargs: Any) -> dict[str, Any]:
        if llm_plan_fn is not None:
            plans = llm_plan_fn(settings, **kwargs)
            if isinstance(plans, list):
                return {"plans": plans, "handoffs": [], "hypotheses": []}
            if isinstance(plans, dict):
                return parse_planner_response(json.dumps(plans))
        return llm_turn_raw(settings, **kwargs)

    role, turn = plan_turn_for_issue(
        settings,
        case,
        issue,
        device_names=device_names,
        llm_turn_fn=_wrap if llm_plan_fn is not None else None,
    )
    return role, list(turn.get("plans") or [])
