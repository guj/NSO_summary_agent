"""Post-loop drill: LLM tool-calling MCP loop → Evidence (heuristics as fallback)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Literal

from agent.config import Settings
from diagnostic_mas.case import (
    CaseFile,
    DrillSession,
    add_evidence,
    begin_drill_issue,
    debit_drill,
)
from diagnostic_mas.deep_checks import DRILL_ALLOWLIST
from multi_agent.base import gate_plan
from multi_agent.deep_checks import run_deep_checks

_PROMPTS = Path(__file__).resolve().parent / "prompts"
_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")
_RESULT_CLIP = 20_000
# Avoid OpenAI SDK default 600s hangs on Fabric after each batch
from agent.summarize import FABRIC_CHAT_TIMEOUT_SEC as _FABRIC_DRILL_TIMEOUT_SEC

DRILL_MCP_CALL_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_call",
        "description": (
            "Call a read-only NSO MCP drill tool. "
            "You may request several mcp_call tools in ONE assistant turn "
            "when you need a batch (e.g. both L2PTP endpoints); results are "
            "returned together before your next decision. "
            "Allowed tool_name: exec_show, get_interface_health, "
            "check_service_sync, get_hardware_health. "
            "For exec_show: params.device_name + params.input_command "
            "(CLI without leading 'show'; never ping/traceroute — those are "
            "not show commands). "
            "check_service_sync requires service_type + service_name "
            "(never device_name alone). "
            "Do not call tools against devices listed in unavailable_devices."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "params": {"type": "object"},
                "reason": {
                    "type": "string",
                    "description": "Short why this check matters for the open issue",
                },
            },
            "required": ["tool_name"],
        },
    },
}

DRILL_CONCLUDE_TOOL = {
    "type": "function",
    "function": {
        "name": "conclude_investigation",
        "description": (
            "End this Issue's drill when the cause is clear (or evidence is "
            "insufficient). Call after you have evaluated the latest tool "
            "batch — not between calls in the same batch. "
            "observed/cause/fix_suggestion must be double-checked against "
            "Evidence (XC vs AC vs seg2; prefer remote=None over equalize local ids)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "observed": {
                    "type": "string",
                    "description": "What was observed (service id, states, key facts)",
                },
                "cause": {
                    "type": "string",
                    "description": "Root cause or best grounded hypothesis",
                },
                "fix_suggestion": {
                    "type": "string",
                    "description": (
                        "Suggested verify/fix command or config change; "
                        "null/omit if unknown"
                    ),
                },
                "fix_requires_human_approval": {
                    "type": "boolean",
                    "description": "True if fix is config (always true for writes)",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                },
            },
            "required": ["observed", "cause"],
        },
    },
}

DRILL_AGENT_TOOLS = [DRILL_MCP_CALL_TOOL, DRILL_CONCLUDE_TOOL]


def _issue_brief(i: dict[str, Any]) -> dict[str, Any]:
    brief: dict[str, Any] = {
        "id": i.get("id"),
        "status": i.get("status"),
        "severity": i.get("severity"),
        "code": i.get("code"),
        "message": i.get("message"),
        "layer": i.get("layer"),
        "edge_id": i.get("edge_id"),
    }
    if i.get("devices"):
        brief["devices"] = list(i["devices"])
    if isinstance(i.get("live_l2"), dict):
        brief["live_l2"] = i["live_l2"]
    if i.get("system_status"):
        brief["system_status"] = i.get("system_status")
    if i.get("dataplane_status"):
        brief["dataplane_status"] = i.get("dataplane_status")
    if "in_sync" in i:
        brief["in_sync"] = i.get("in_sync")
    if isinstance(i.get("device_sync"), dict):
        brief["device_sync"] = i["device_sync"]
    return brief


def _clip_prior_text(value: Any, *, limit: int = 600) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str)
        except TypeError:
            text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _evidence_tied_to_issue(ev: dict[str, Any], issue: dict[str, Any]) -> bool:
    """True if evidence belongs to this Issue / service edge."""
    iid = str(issue.get("id") or "")
    edge = str(issue.get("edge_id") or "")
    devices = {
        str(d)
        for d in (issue.get("devices") or [])
        if isinstance(d, str) and d.strip()
    }
    live = issue.get("live_l2") if isinstance(issue.get("live_l2"), dict) else {}
    for ep in live.get("endpoints") or []:
        if isinstance(ep, dict):
            d = ep.get("device")
            if isinstance(d, str) and d.strip():
                devices.add(d.strip())
    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
    if iid and ev.get("id") in set(issue.get("evidence_ids") or []):
        return True
    if iid and payload.get("issue_id") == iid:
        return True
    if edge and payload.get("issue_edge_id") == edge:
        return True
    if edge and payload.get("name") == edge:
        return True
    if edge and payload.get("service_name") == edge:
        return True
    subject = payload.get("subject") if isinstance(payload.get("subject"), dict) else {}
    if edge and subject.get("name") == edge:
        return True
    kind = ev.get("kind")
    if kind in {"dataplane_finding", "dataplane_incomplete"}:
        if edge and (payload.get("name") == edge or payload.get("service_name") == edge):
            return True
    if kind in {"drill", "deep_check"} and devices:
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        plan = payload.get("plan") if isinstance(payload.get("plan"), list) else []
        device = args.get("device") or args.get("device_name")
        if isinstance(device, str) and device in devices:
            return True
        for task in plan:
            if not isinstance(task, dict):
                continue
            targs = task.get("args") if isinstance(task.get("args"), dict) else {}
            d = targs.get("device") or targs.get("device_name")
            if isinstance(d, str) and d in devices:
                return True
            if task.get("issue_id") == iid or task.get("issue_edge_id") == edge:
                return True
    return False


def _probe_fingerprint_from_payload(payload: dict[str, Any]) -> str | None:
    check = str(payload.get("check") or "").strip()
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    device = str(args.get("device") or args.get("device_name") or "").strip()
    if not check:
        return None
    cmd = str(args.get("input_command") or args.get("command") or "").strip().lower()
    cmd = re.sub(r"^show\s+", "", cmd)
    cmd = re.sub(r"\s+", " ", cmd)
    bits = [check]
    if device:
        bits.append(device)
    if cmd:
        bits.append(cmd)
    return " ".join(bits)


def prior_exploration_for_issue(
    case: CaseFile, issue: dict[str, Any]
) -> dict[str, Any]:
    """Findings / probes already collected for this Issue — avoid re-running them."""
    iid = str(issue.get("id") or "")
    edge = str(issue.get("edge_id") or "")

    diagnoses: list[dict[str, Any]] = []
    for dx in case.diagnoses:
        if dx.get("kind") != "dataplane":
            continue
        subject = dx.get("subject") if isinstance(dx.get("subject"), dict) else {}
        if edge and subject.get("name") == edge:
            diagnoses.append(
                {
                    "status": dx.get("status"),
                    "source": dx.get("source"),
                    "complete": dx.get("complete", True),
                    "observed": dx.get("observed"),
                    "cause": dx.get("cause"),
                }
            )

    hypotheses = [
        {"text": h.get("text")}
        for h in case.hypotheses
        if isinstance(h, dict)
        and (
            h.get("issue_id") == iid
            or iid in (h.get("issue_ids") or [])
            or (edge and edge in str(h.get("text") or ""))
        )
    ][:10]

    already_run: list[str] = []
    seen_fp: set[str] = set()
    tool_traces: list[dict[str, Any]] = []
    for ev in case.evidence:
        if not isinstance(ev, dict):
            continue
        if not _evidence_tied_to_issue(ev, issue):
            continue
        kind = ev.get("kind")
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if kind in {"drill", "deep_check"}:
            fp = _probe_fingerprint_from_payload(payload)
            if fp and fp not in seen_fp:
                seen_fp.add(fp)
                already_run.append(fp)
            if kind == "drill":
                tool_traces.append(
                    {
                        "check": payload.get("check"),
                        "args": payload.get("args"),
                        "reason": payload.get("reason"),
                        "error": payload.get("error"),
                        "result": _clip_prior_text(payload.get("result"), limit=500),
                    }
                )
            elif kind == "deep_check":
                for task, result in zip(
                    payload.get("plan") or [],
                    payload.get("results") or [],
                    strict=False,
                ):
                    if not isinstance(task, dict):
                        continue
                    targs = task.get("args") if isinstance(task.get("args"), dict) else {}
                    fp2 = _probe_fingerprint_from_payload(
                        {"check": task.get("check"), "args": targs}
                    )
                    if fp2 and fp2 not in seen_fp:
                        seen_fp.add(fp2)
                        already_run.append(fp2)
                    tool_traces.append(
                        {
                            "check": task.get("check"),
                            "args": targs,
                            "result": _clip_prior_text(result, limit=500),
                        }
                    )
        if kind in {"dataplane_finding", "dataplane_incomplete", "drill_finding"}:
            tool_traces.append(
                {
                    "kind": kind,
                    "observed": payload.get("observed"),
                    "cause": payload.get("cause"),
                    "dataplane_status": payload.get("dataplane_status")
                    or payload.get("status"),
                    "source": payload.get("source"),
                }
            )

    return {
        "note": (
            "Do not repeat probes in already_run_probes; ground new calls on gaps only."
        ),
        "diagnoses": diagnoses[:5],
        "hypotheses": hypotheses,
        "already_run_probes": already_run[:40],
        "prior_tool_traces": tool_traces[:20],
    }


# Inventory / mapping noise — keep on Issues list, do not spend drill LLM tokens.
_DRILL_SKIP_CODES = frozenset(
    {
        "unknown_neighbor_address",
        "unknown_neighbor_system_id",
    }
)

# Eligible for drill. ``open`` outranks ``budget_exhausted`` in priority.
_DRILL_STATUSES = frozenset({"open", "explained", "needs_human", "budget_exhausted"})
_DRILL_STATUS_RANK = {
    "open": 3,
    "needs_human": 2,
    "explained": 1,
    "budget_exhausted": 0,
}


def _drill_issue_priority(issue: dict[str, Any]) -> tuple:
    """Higher sort key = drill sooner.

    Prefer ``open`` over ``budget_exhausted``, then live_l2 DN / degraded / severity.
    """
    status_rank = _DRILL_STATUS_RANK.get(str(issue.get("status") or "").lower(), -1)
    sev = {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(
        str(issue.get("severity") or "").lower(), 0
    )
    live = issue.get("live_l2") if isinstance(issue.get("live_l2"), dict) else {}
    has_dn = 0
    for ep in live.get("endpoints") or []:
        if not isinstance(ep, dict):
            continue
        st = str(ep.get("st") or "").upper()
        if st == "DN" or ep.get("error") in {"ac_not_found", "no_xconnect_data"}:
            has_dn = 1
            break
    code = str(issue.get("code") or "")
    degraded = 1 if "degraded" in code or "down" in code else 0
    return (status_rank, has_dn, degraded, sev)


def _quarantined_device_map() -> dict[str, str]:
    try:
        from nso_facts.mcp_client import quarantined_devices

        return dict(quarantined_devices() or {})
    except Exception:  # noqa: BLE001
        return {}


def _issue_live_devices(issue: dict[str, Any]) -> list[str]:
    """Issue devices that are not quarantined this run."""
    q = _quarantined_device_map()
    out: list[str] = []
    for d in issue.get("devices") or []:
        name = str(d).strip()
        if name and name not in q:
            out.append(name)
    return out


def select_drill_issues(case: CaseFile, *, limit: int) -> list[dict[str, Any]]:
    """Pick up to ``limit`` Issues that warrant a drill session.

    Prefer issues with at least one non-quarantined device. Skip issues whose
    listed devices are all quarantined (no live MCP possible).
    """
    if limit <= 0:
        return []
    from diagnostic_mas.dataplane_verify import dataplane_diagnosed_names

    dataplane_done = dataplane_diagnosed_names(case)
    candidates = [
        i
        for i in case.issues
        if i.get("status") in _DRILL_STATUSES
        and str(i.get("code") or "") not in _DRILL_SKIP_CODES
        and not (
            i.get("layer") == "services"
            and isinstance(i.get("edge_id"), str)
            and i.get("edge_id") in dataplane_done
        )
    ]
    live: list[dict[str, Any]] = []
    no_device: list[dict[str, Any]] = []
    for issue in candidates:
        devices = [str(d).strip() for d in (issue.get("devices") or []) if d]
        if not devices:
            no_device.append(issue)
            continue
        if _issue_live_devices(issue):
            live.append(issue)
        # else: all endpoints quarantined — skip (availability-aware)
    live.sort(key=_drill_issue_priority, reverse=True)
    no_device.sort(key=_drill_issue_priority, reverse=True)
    ordered = live + no_device
    return ordered[:limit]


def compact_drill_context(
    case: CaseFile,
    *,
    focus_issue: dict[str, Any] | None = None,
    remaining_tools: int | None = None,
) -> dict[str, Any]:
    """Compact Issue(s) + remaining tool budget for the drill agent."""
    if remaining_tools is None:
        remaining_tools = max(0, case.budget.max_tools_per_drill)
    if focus_issue is not None:
        issues_brief = [_issue_brief(focus_issue)]
    else:
        issues_brief = [
            _issue_brief(i)
            for i in case.issues
            if i.get("status") in _DRILL_STATUSES
        ][:40]
    quarantined = _quarantined_device_map()
    known = sorted(getattr(case, "device_names", None) or [])[:80]
    available = [d for d in known if d not in quarantined]
    out: dict[str, Any] = {
        "remaining_tool_calls": remaining_tools,
        "max_tools_per_drill": case.budget.max_tools_per_drill,
        "max_drill_issues": case.budget.max_drill_issues,
        "drill_issues_used": case.budget.drill_issues_used,
        "allowlist": sorted(DRILL_ALLOWLIST),
        "known_devices": known,
        "available_devices": available[:80],
        "unavailable_devices": sorted(quarantined.keys()),
        "issues": issues_brief,
        "evidence": [
            {
                "kind": e.get("kind"),
                "role": e.get("role"),
                "layer": e.get("layer"),
            }
            for e in case.evidence[:40]
        ],
    }
    if focus_issue is not None:
        out["prior_exploration"] = prior_exploration_for_issue(case, focus_issue)
        live = _issue_live_devices(focus_issue)
        out["focus_live_devices"] = live
        if focus_issue.get("devices") and not live:
            out["availability_note"] = (
                "All listed endpoints for this Issue are quarantined this run; "
                "do not call MCP on them — conclude with uncertainty from "
                "existing Evidence."
            )
    return out


def parse_drill_plans(text: str) -> list[dict[str, Any]]:
    """Parse legacy JSON plan lists (heuristics / tests)."""
    raw = (text or "").strip()
    if not raw:
        return []
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
            arr = _JSON_ARRAY_RE.search(raw)
            if arr:
                try:
                    data = json.loads(arr.group(0))
                except json.JSONDecodeError:
                    return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        plans = data.get("plans")
        if isinstance(plans, list):
            return [x for x in plans if isinstance(x, dict)]
    return []


def heuristic_live_l2_drill_plans(
    case: CaseFile,
    *,
    remaining: int,
    focus_issue: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build verify drills from live_l2 DN endpoints (wave 1 fallback).

    Priority within ``remaining`` (DN side only — peer/BGP/IS-IS is wave 2):
    1. ``interfaces <ac>`` on each DN endpoint
    2. ``l2vpn xconnect`` on each DN device (once per device)
    """
    if remaining <= 0:
        return []
    plans: list[dict[str, Any]] = []
    dn_eps: list[tuple[str, str, str | None]] = []  # device, ac, edge_id

    issues = [focus_issue] if focus_issue is not None else case.issues
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if issue.get("status") not in _DRILL_STATUSES:
            continue
        live = issue.get("live_l2")
        if not isinstance(live, dict):
            continue
        edge_id = issue.get("edge_id")
        for ep in live.get("endpoints") or []:
            if not isinstance(ep, dict):
                continue
            device = str(ep.get("device") or "").strip()
            ac = str(ep.get("ac") or "").strip()
            st = str(ep.get("st") or "").upper()
            if not device or not ac:
                continue
            if st == "DN" or ep.get("error") in {"ac_not_found", "no_xconnect_data"}:
                dn_eps.append((device, ac, str(edge_id) if edge_id else None))

    seen_xc: set[str] = set()
    for device, ac, edge_id in dn_eps:
        if len(plans) >= remaining:
            break
        plans.append(
            {
                "check": "exec_show",
                "args": {
                    "device_name": device,
                    "input_command": f"interfaces {ac}",
                },
                "reason": f"auto-verify DN AC {ac}",
                "issue_edge_id": edge_id,
                "layer": "services",
            }
        )
        if len(plans) >= remaining:
            break
        if device not in seen_xc:
            seen_xc.add(device)
            plans.append(
                {
                    "check": "exec_show",
                    "args": {
                        "device_name": device,
                        "input_command": "l2vpn xconnect",
                    },
                    "reason": f"auto-verify xconnect on {device}",
                    "issue_edge_id": edge_id,
                    "layer": "services",
                }
            )

    return plans[:remaining]


def _drill_result_text(payload: dict[str, Any]) -> str:
    result = payload.get("result")
    err = payload.get("error")
    parts: list[str] = []
    if err:
        parts.append(str(err))
    if result is None:
        return " ".join(parts)
    if isinstance(result, str):
        parts.append(result)
    else:
        parts.append(json.dumps(result, default=str))
    return "\n".join(parts)


def _iface_looks_up_up(text: str) -> bool:
    t = (text or "").lower()
    if not t:
        return False
    # XR-style: "line protocol is up" + admin up, or compact "up/up"
    if "up/up" in t.replace(" ", ""):
        return True
    admin_up = "is up" in t or "administratively up" in t or re.search(
        r"\bup\b.*\bup\b", t
    )
    line_up = "line protocol is up" in t or "protocol is up" in t
    return bool(admin_up and line_up) or (
        "line protocol is up" in t and "is down" not in t[:200]
    )


def _xconnect_ac_dn(text: str, ac: str) -> bool:
    from nso_facts.l2vpn_xconnect import find_xconnect_row, parse_l2vpn_xconnect

    rows = parse_l2vpn_xconnect(text or "")
    row = find_xconnect_row(ac, rows)
    if not row:
        return False
    return str(row.get("st") or "").upper() == "DN"


def find_iface_up_xconnect_dn_targets(case: CaseFile) -> list[dict[str, str]]:
    """Return [{device, ac, edge_id?}] where XC DN but local AC is UP.

    Triggers from:
    - live_l2 endpoint with st=DN and ac_st=UP (parsed xconnect detail), or
    - drills: interfaces up/up + xconnect row DN for that AC.
    """
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(device: str, ac: str, edge: str) -> None:
        key = (device, ac)
        if key in seen or not device or not ac:
            return
        seen.add(key)
        out.append({"device": device, "ac": ac, "edge_id": edge})

    for issue in case.issues:
        live = issue.get("live_l2")
        if not isinstance(live, dict):
            continue
        edge = str(issue.get("edge_id") or "")
        for ep in live.get("endpoints") or []:
            if not isinstance(ep, dict):
                continue
            device = str(ep.get("device") or "").strip()
            ac = str(ep.get("ac") or "").strip()
            st = str(ep.get("st") or "").upper()
            ac_st = str(ep.get("ac_st") or "").upper()
            if device and ac and st == "DN" and ac_st == "UP":
                _add(device, ac, edge)

    # Drill-confirmed: iface up/up + XC DN (when live_l2 lacked ac_st)
    dn_eps: list[dict[str, str]] = []
    for issue in case.issues:
        live = issue.get("live_l2")
        if not isinstance(live, dict):
            continue
        edge = str(issue.get("edge_id") or "")
        for ep in live.get("endpoints") or []:
            if not isinstance(ep, dict):
                continue
            st = str(ep.get("st") or "").upper()
            if st != "DN" and ep.get("error") not in {
                "ac_not_found",
                "no_xconnect_data",
            }:
                continue
            device = str(ep.get("device") or "").strip()
            ac = str(ep.get("ac") or "").strip()
            if device and ac:
                dn_eps.append({"device": device, "ac": ac, "edge_id": edge})

    from nso_facts.l2vpn_xconnect import find_xconnect_row, parse_l2vpn_xconnect

    for ep in dn_eps:
        device, ac = ep["device"], ep["ac"]
        if (device, ac) in seen:
            continue
        iface_up = False
        xc_dn = False
        for ev in case.evidence:
            if ev.get("kind") != "drill":
                continue
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
            cmd = str(args.get("input_command") or args.get("command") or "").lower()
            d = str(args.get("device_name") or args.get("device") or "")
            if d != device:
                continue
            text = _drill_result_text(payload)
            if "interfaces" in cmd and ac.lower() in cmd.lower():
                if _iface_looks_up_up(text):
                    iface_up = True
            if "l2vpn" in cmd and "xconnect" in cmd:
                if _xconnect_ac_dn(text, ac):
                    xc_dn = True
                row = find_xconnect_row(ac, parse_l2vpn_xconnect(text))
                if row and str(row.get("st") or "").upper() == "DN":
                    xc_dn = True
                    if str(row.get("ac_st") or "").upper() == "UP":
                        iface_up = True
        if iface_up and xc_dn:
            _add(device, ac, ep.get("edge_id") or "")

    return out


def heuristic_wave2_drill_plans(
    case: CaseFile, *, remaining: int
) -> list[dict[str, Any]]:
    """After iface UP + xconnect DN: dig into peer AC, BGP, IS-IS (fallback)."""
    if remaining <= 0:
        return []
    targets = find_iface_up_xconnect_dn_targets(case)
    if not targets:
        return []

    # Peer UP endpoints from same issues
    peers: list[tuple[str, str, str]] = []
    for issue in case.issues:
        live = issue.get("live_l2")
        if not isinstance(live, dict):
            continue
        edge = str(issue.get("edge_id") or "")
        for ep in live.get("endpoints") or []:
            if not isinstance(ep, dict):
                continue
            if str(ep.get("st") or "").upper() != "UP":
                continue
            device = str(ep.get("device") or "").strip()
            ac = str(ep.get("ac") or "").strip()
            if device and ac:
                peers.append((device, ac, edge))

    plans: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(plan: dict[str, Any]) -> None:
        if len(plans) >= remaining:
            return
        key = json.dumps(plan.get("args") or {}, sort_keys=True) + str(plan.get("check"))
        if key in seen:
            return
        seen.add(key)
        plans.append(plan)

    # Prefer dig on the DN device first (BGP / IS-IS), then peer verifies
    for ep in targets:
        device = ep["device"]
        edge = ep.get("edge_id") or None
        _add(
            {
                "check": "exec_show",
                "args": {"device_name": device, "input_command": "bgp summary"},
                "reason": "wave2: EVPN/BGP after iface UP + XC DN",
                "issue_edge_id": edge,
                "layer": "routing",
            }
        )
        _add(
            {
                "check": "exec_show",
                "args": {
                    "device_name": device,
                    "input_command": "isis neighbors",
                },
                "reason": "wave2: underlay after iface UP + XC DN",
                "issue_edge_id": edge,
                "layer": "underlay",
            }
        )

    for device, ac, edge in peers:
        _add(
            {
                "check": "exec_show",
                "args": {"device_name": device, "input_command": f"interfaces {ac}"},
                "reason": "wave2: verify peer AC after local iface UP + XC DN",
                "issue_edge_id": edge or None,
                "layer": "services",
            }
        )
        _add(
            {
                "check": "exec_show",
                "args": {"device_name": device, "input_command": "l2vpn xconnect"},
                "reason": "wave2: peer xconnect after local iface UP + XC DN",
                "issue_edge_id": edge or None,
                "layer": "services",
            }
        )

    # Record a deterministic finding for Summary
    if targets:
        add_evidence(
            case,
            {
                "kind": "drill",
                "role": "drill",
                "layer": "services",
                "payload": {
                    "check": "analysis",
                    "args": {},
                    "reason": "wave2 trigger",
                    "result": {
                        "finding": "interface_up_xconnect_dn",
                        "targets": targets,
                        "note": (
                            "XConnect DN while local AC ST is UP (or interface up/up) — "
                            "not a simple physical AC-down; dig EVPN/remote and underlay "
                            "(wave2 drills)."
                        ),
                    },
                },
            },
        )

    return plans[:remaining]


def _clip_tool_content(value: Any, limit: int = _RESULT_CLIP) -> str:
    from nso_facts.mcp_client import unwrap_mcp_result_text

    text = unwrap_mcp_result_text(value)
    if len(text) <= limit:
        return text
    keep = max(0, limit - 72)
    return (
        text[:keep].rstrip()
        + f"\n\n[truncated: showing {keep} of {len(text)} chars]"
    )


def _message_to_dict(message: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "role": "assistant",
        "content": message.content,
    }
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        serialized = []
        for call in tool_calls:
            serialized.append(
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
            )
        data["tool_calls"] = serialized
    return data


def _system_prompt_drill_agent() -> str:
    prompt_path = _PROMPTS / "drill_agent.txt"
    if prompt_path.is_file():
        return prompt_path.read_text(encoding="utf-8")
    return (
        "Investigate Issues via mcp_call (batch when needed). "
        "When cause is clear, call conclude_investigation."
    )


def parse_drill_finding(data: Any) -> dict[str, Any] | None:
    """Normalize conclude / final JSON into a finding dict."""
    if isinstance(data, str):
        raw = data.strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            obj = _JSON_OBJECT_RE.search(raw)
            if not obj:
                return None
            try:
                data = json.loads(obj.group(0))
            except json.JSONDecodeError:
                return None
    if not isinstance(data, dict):
        return None
    observed = data.get("observed")
    cause = data.get("cause")
    if not (isinstance(observed, str) and observed.strip()):
        return None
    if not (isinstance(cause, str) and cause.strip()):
        return None
    fix = data.get("fix_suggestion")
    if fix is not None and not isinstance(fix, str):
        fix = str(fix)
    conf = str(data.get("confidence") or "medium").lower()
    if conf not in {"high", "medium", "low"}:
        conf = "medium"
    return {
        "observed": observed.strip(),
        "cause": cause.strip(),
        "fix_suggestion": fix.strip() if isinstance(fix, str) and fix.strip() else None,
        "fix_requires_human_approval": bool(
            data.get("fix_requires_human_approval", True)
        ),
        "confidence": conf,
    }


def record_drill_finding(
    case: CaseFile,
    finding: dict[str, Any],
    *,
    session: DrillSession | None = None,
) -> None:
    add_evidence(
        case,
        {
            "kind": "drill_finding",
            "role": "drill",
            "layer": "services",
            "payload": {
                **finding,
                "issue_id": session.issue_id if session else None,
                "issue_edge_id": session.issue_edge_id if session else None,
            },
        },
    )


def _log_drill(msg: str) -> None:
    print(f"[drill] {msg}", file=sys.stderr)


async def execute_drill_plans(
    client: Any,
    case: CaseFile,
    plans: list[dict[str, Any]],
    *,
    device_names: set[str],
    session: DrillSession | None = None,
) -> list[dict[str, Any]]:
    if session is not None:
        remaining = session.max_tools - session.tools_used
    else:
        remaining = (
            case.budget.max_drill_issues * case.budget.max_tools_per_drill
            - case.budget.drills_used
        )
    if remaining <= 0:
        return []
    gated = gate_plan(
        list(plans or []),
        allowlist=DRILL_ALLOWLIST,
        device_names=device_names,
        max_tasks=remaining,
    )
    accepted: list[dict[str, Any]] = []
    for task in gated:
        if not debit_drill(case, 1, session=session):
            break
        accepted.append(task)
    if not accepted:
        return []
    results = await run_deep_checks(client, accepted)
    for idx, task in enumerate(accepted):
        result = results[idx] if idx < len(results) else {}
        if not isinstance(result, dict):
            result = {"result": result}
        add_evidence(
            case,
            {
                "kind": "drill",
                "role": "drill",
                "layer": str(task.get("layer") or "services"),
                "payload": {
                    "check": task.get("check"),
                    "args": task.get("args"),
                    "reason": task.get("reason"),
                    "issue_edge_id": task.get("issue_edge_id") or task.get("issue_id"),
                    "issue_id": session.issue_id if session else None,
                    "result": result.get("result"),
                    "error": result.get("error"),
                },
            },
        )
    return results


async def execute_one_drill_call(
    client: Any,
    case: CaseFile,
    *,
    tool_name: str,
    params: dict[str, Any] | None,
    reason: str | None,
    device_names: set[str],
    session: DrillSession | None = None,
    allowlist: frozenset[str] | None = None,
    account: str = "drill",
) -> str:
    """Gate + debit + MCP one drill; return text for the LLM tool role."""
    allowed = allowlist if allowlist is not None else DRILL_ALLOWLIST
    if session is not None:
        remaining = session.max_tools - session.tools_used
    else:
        remaining = (
            case.budget.max_drill_issues * case.budget.max_tools_per_drill
            - case.budget.drills_used
        )
    if remaining <= 0:
        return "ERROR: drill tool budget exhausted"
    check = str(tool_name or "").strip()
    args = dict(params or {})
    from multi_agent.base import task_rejection_reason

    reject = task_rejection_reason(
        check,
        args,
        allowlist=allowed,
        device_names=device_names,
    )
    if reject:
        return f"ERROR: {reject}"
    gated = gate_plan(
        [{"check": check, "args": args, "reason": reason}],
        allowlist=allowed,
        device_names=device_names,
        max_tasks=1,
    )
    if not gated:
        return (
            f"ERROR: tool {check!r} rejected "
            f"(allowlist/device/args). Allowlist={sorted(allowed)}"
        )
    task = gated[0]
    acct: Literal["drill", "dataplane"] = (
        "dataplane" if account == "dataplane" else "drill"
    )
    if not debit_drill(case, 1, session=session, account=acct):
        return "ERROR: drill tool budget exhausted"
    results = await run_deep_checks(client, [task])
    result = results[0] if results else {}
    if not isinstance(result, dict):
        result = {"result": result}
    add_evidence(
        case,
        {
            "kind": "drill",
            "role": "drill",
            "layer": "services",
            "payload": {
                "check": task.get("check"),
                "args": task.get("args"),
                "reason": reason or task.get("reason"),
                "issue_id": session.issue_id if session else None,
                "issue_edge_id": session.issue_edge_id if session else None,
                "result": result.get("result"),
                "error": result.get("error"),
            },
        },
    )
    if result.get("error"):
        return _clip_tool_content({"error": result.get("error")})
    return _clip_tool_content(result.get("result"))


async def llm_drill_tool_loop(
    client: Any,
    settings: Settings,
    case: CaseFile,
    *,
    device_names: set[str],
    focus_issue: dict[str, Any],
    session: DrillSession,
    openai_client: Any | None = None,
) -> tuple[int, bool]:
    """Batch tool calls → then LLM evaluates; conclude ends the loop.

    Returns ``(tools_executed, concluded)``.
    """
    from agent.summarize import fabric_openai_client

    before = session.tools_used
    remaining = session.max_tools - session.tools_used
    if remaining <= 0:
        return 0, False

    edge = focus_issue.get("edge_id") or focus_issue.get("id") or "?"
    _log_drill(f"start issue={edge} tools_budget={remaining}")

    ctx = compact_drill_context(
        case, focus_issue=focus_issue, remaining_tools=remaining
    )
    ctx["known_devices"] = sorted(device_names)[:80]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt_drill_agent()},
        {
            "role": "user",
            "content": (
                "Investigate this single Issue. "
                "Batch mcp_call tools when you need multiple facts at once "
                "(e.g. both service endpoints). "
                "Use available_devices / focus_live_devices only — never call "
                "tools on unavailable_devices (quarantined). "
                "check_service_sync needs service_type + service_name; "
                "never ping/traceroute via exec_show. "
                "Use prior_exploration (diagnoses, already_run_probes, "
                "prior_tool_traces) — do NOT repeat those probes. "
                "After you have enough to name the cause, call "
                "conclude_investigation "
                f"(observed / cause / optional fix). "
                f"At most {remaining} mcp_call tools for this Issue.\n\n"
                f"{json.dumps(ctx, indent=2, default=str)}"
            ),
        },
    ]
    oai = openai_client or fabric_openai_client(
        settings, timeout=_FABRIC_DRILL_TIMEOUT_SEC
    )
    max_rounds = remaining + 2
    concluded = False
    # After the last mcp_call depletes the budget, allow one more LLM turn so
    # conclude_investigation can evaluate that result (do not break on remaining==0).
    await_conclusion = False

    for round_i in range(max_rounds):
        remaining = session.max_tools - session.tools_used
        if remaining <= 0 and not await_conclusion:
            break
        if concluded:
            break
        _log_drill(
            f"LLM round {round_i + 1} (tools_left={remaining}"
            f"{', conclude_only' if remaining <= 0 else ''})"
        )
        if remaining <= 0:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Tool budget is exhausted. Call conclude_investigation now "
                        "using the evidence already returned "
                        "(state uncertainty in cause if unsure)."
                    ),
                }
            )
        await_conclusion = False
        try:
            response = oai.chat.completions.create(
                model=settings.fabric_model,
                messages=messages,
                tools=DRILL_AGENT_TOOLS,
                tool_choice="auto",
                temperature=0.1,
            )
        except Exception as exc:  # noqa: BLE001
            from agent.llm_budget import (
                is_provider_budget_exceeded,
                mark_case_llm_halt,
                provider_budget_message,
            )

            if is_provider_budget_exceeded(exc):
                msg = provider_budget_message(exc)
                mark_case_llm_halt(case, f"provider budget_exceeded: {msg}")
                _log_drill(
                    f"LLM budget exceeded — stopping further drills: {exc}"
                )
                break
            _log_drill(f"LLM error: {exc}")
            break
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            finding = parse_drill_finding(message.content or "")
            if finding:
                record_drill_finding(case, finding, session=session)
                _log_drill(f"concluded via content: {finding.get('cause', '')[:80]}")
                concluded = True
            else:
                _log_drill("LLM returned no tools / no finding — stop")
            break

        messages.append(_message_to_dict(message))
        for call in tool_calls:
            fn = call.function
            fn_name = getattr(fn, "name", "") or ""
            raw_args = getattr(fn, "arguments", "") or "{}"
            try:
                args = (
                    json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                )
            except json.JSONDecodeError:
                args = {}

            if fn_name == "conclude_investigation":
                finding = parse_drill_finding(args)
                if finding:
                    record_drill_finding(case, finding, session=session)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps({"ok": True, "recorded": True}),
                        }
                    )
                    _log_drill(f"conclude: {finding.get('cause', '')[:100]}")
                    concluded = True
                else:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": (
                                "ERROR: conclude_investigation needs "
                                "observed + cause strings"
                            ),
                        }
                    )
                continue

            if fn_name != "mcp_call":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": (
                            f"ERROR: unknown function {fn_name!r}; "
                            "use mcp_call or conclude_investigation"
                        ),
                    }
                )
                continue

            remaining = session.max_tools - session.tools_used
            if remaining <= 0:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": (
                            "ERROR: drill tool budget exhausted — "
                            "call conclude_investigation now"
                        ),
                    }
                )
                await_conclusion = True
                continue

            tool_name = str(args.get("tool_name") or "")
            params = (
                args.get("params") if isinstance(args.get("params"), dict) else {}
            )
            reason = args.get("reason")
            from diagnostic_mas.dataplane_verify import (
                coerce_dataplane_mcp_call,
                empty_mcp_tool_name_error,
            )

            tool_name, params, coerce_note = coerce_dataplane_mcp_call(
                tool_name, params
            )
            if coerce_note:
                _log_drill(f"mcp_call coerce: {coerce_note}")
            if not tool_name:
                _log_drill(
                    "blocked empty tool_name "
                    f"{json.dumps(params, default=str)[:120]}"
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": empty_mcp_tool_name_error(params),
                    }
                )
                continue
            _log_drill(f"mcp_call {tool_name} {json.dumps(params, default=str)[:120]}")
            result_text = await execute_one_drill_call(
                client,
                case,
                tool_name=tool_name,
                params=params,
                reason=str(reason) if reason else None,
                device_names=device_names,
                session=session,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result_text,
                }
            )
            # Last debit may leave remaining==0 — still need an eval round.
            if session.tools_used >= session.max_tools:
                await_conclusion = True

        if concluded:
            break

    if not concluded:
        _log_drill(f"issue={edge} ended without conclude_investigation")
    return session.tools_used - before, concluded


def _filter_plans_already_run(
    case: CaseFile,
    plans: list[dict[str, Any]],
    *,
    focus_issue: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Drop heuristic plans that duplicate prior exploration probes."""
    if not plans:
        return plans
    already: set[str] = set()
    issues = [focus_issue] if focus_issue is not None else list(case.issues)
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        prior = prior_exploration_for_issue(case, issue)
        for fp in prior.get("already_run_probes") or []:
            if isinstance(fp, str) and fp:
                already.add(fp)
    if not already:
        return plans
    kept: list[dict[str, Any]] = []
    for plan in plans:
        fp = _probe_fingerprint_from_payload(
            {
                "check": plan.get("check"),
                "args": plan.get("args") if isinstance(plan.get("args"), dict) else {},
            }
        )
        if fp and fp in already:
            continue
        kept.append(plan)
    return kept


async def run_heuristic_drill_fallback(
    client: Any,
    case: CaseFile,
    *,
    device_names: set[str],
    focus_issue: dict[str, Any] | None = None,
    session: DrillSession | None = None,
) -> None:
    """Wave-1 then wave-2 when the LLM tool loop executed nothing for an issue."""
    if session is None:
        session = DrillSession(max_tools=case.budget.max_tools_per_drill)
    remaining = session.max_tools - session.tools_used
    if remaining <= 0:
        return
    plans = _filter_plans_already_run(
        case,
        heuristic_live_l2_drill_plans(
            case, remaining=remaining, focus_issue=focus_issue
        ),
        focus_issue=focus_issue,
    )
    if plans:
        await execute_drill_plans(
            client, case, plans, device_names=device_names, session=session
        )

    remaining = session.max_tools - session.tools_used
    if remaining <= 0:
        return
    wave2 = _filter_plans_already_run(
        case,
        heuristic_wave2_drill_plans(case, remaining=remaining),
        focus_issue=focus_issue,
    )
    if wave2:
        await execute_drill_plans(
            client, case, wave2, device_names=device_names, session=session
        )


async def run_drill_phase(
    client: Any,
    settings: Settings,
    case: CaseFile,
    *,
    device_names: set[str],
    skip_llm: bool = False,
    tool_loop_fn: Any | None = None,
    openai_client: Any | None = None,
    # Back-compat for older tests that injected a JSON planner
    llm_plan_fn: Any | None = None,
) -> None:
    """Drill up to max_drill_issues; each gets max_tools_per_drill tool calls."""
    if skip_llm:
        return
    if getattr(case, "llm_halt_reason", None):
        _log_drill(
            f"skip drill phase — provider LLM halted ({case.llm_halt_reason})"
        )
        return
    if case.budget.max_drill_issues <= 0 or case.budget.max_tools_per_drill <= 0:
        return
    if (
        tool_loop_fn is None
        and llm_plan_fn is None
        and not getattr(settings, "fabric_api_key", None)
        and openai_client is None
    ):
        return

    # Injected whole-case tool loop (tests): one shot, then optional fallback
    if tool_loop_fn is not None:
        before = case.budget.drills_used
        result = tool_loop_fn(client, settings, case, device_names=device_names)
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]
        if case.budget.drills_used <= before:
            targets = select_drill_issues(case, limit=case.budget.max_drill_issues)
            for issue in targets:
                if not begin_drill_issue(case):
                    break
                session = DrillSession(
                    max_tools=case.budget.max_tools_per_drill,
                    issue_id=str(issue.get("id") or "") or None,
                    issue_edge_id=str(issue.get("edge_id") or "") or None,
                )
                await run_heuristic_drill_fallback(
                    client,
                    case,
                    device_names=device_names,
                    focus_issue=issue,
                    session=session,
                )
        return

    if llm_plan_fn is not None:
        before = case.budget.drills_used
        plans = llm_plan_fn(settings, case, device_names=device_names)
        plans = list(plans) if isinstance(plans, list) else []
        if plans:
            session = DrillSession(max_tools=case.budget.max_tools_per_drill)
            if begin_drill_issue(case):
                await execute_drill_plans(
                    client, case, plans, device_names=device_names, session=session
                )
        if case.budget.drills_used <= before:
            targets = select_drill_issues(case, limit=case.budget.max_drill_issues)
            for issue in targets:
                if not begin_drill_issue(case):
                    break
                session = DrillSession(
                    max_tools=case.budget.max_tools_per_drill,
                    issue_id=str(issue.get("id") or "") or None,
                    issue_edge_id=str(issue.get("edge_id") or "") or None,
                )
                await run_heuristic_drill_fallback(
                    client,
                    case,
                    device_names=device_names,
                    focus_issue=issue,
                    session=session,
                )
        return

    targets = select_drill_issues(case, limit=case.budget.max_drill_issues)
    for issue in targets:
        if getattr(case, "llm_halt_reason", None):
            _log_drill(
                f"stop remaining drills — provider LLM halted "
                f"({case.llm_halt_reason})"
            )
            break
        if not begin_drill_issue(case):
            break
        session = DrillSession(
            max_tools=case.budget.max_tools_per_drill,
            issue_id=str(issue.get("id") or "") or None,
            issue_edge_id=str(issue.get("edge_id") or "") or None,
        )
        before = session.tools_used
        concluded = False
        try:
            _n, concluded = await llm_drill_tool_loop(
                client,
                settings,
                case,
                device_names=device_names,
                focus_issue=issue,
                session=session,
                openai_client=openai_client,
            )
        except Exception as exc:  # noqa: BLE001
            from agent.llm_budget import is_provider_budget_exceeded

            concluded = False
            if is_provider_budget_exceeded(exc) or getattr(
                case, "llm_halt_reason", None
            ):
                break
        if getattr(case, "llm_halt_reason", None):
            break
        if not concluded and session.tools_used <= before:
            await run_heuristic_drill_fallback(
                client,
                case,
                device_names=device_names,
                focus_issue=issue,
                session=session,
            )
