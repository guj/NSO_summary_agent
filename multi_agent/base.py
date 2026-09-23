"""Shared types, allowlists, and plan gating for multi-agent experiment."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

ISIS_ALLOWLIST = frozenset({"exec_show"})
BGP_ALLOWLIST = frozenset({"exec_show", "verify_bgp_peer_reachability"})
DEVICE_ALLOWLIST = frozenset(
    {"exec_show", "get_hardware_health", "get_interface_health"}
)

_MAX_TASKS_DEFAULT = 5

_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")

# exec_show prepends "show "; these are not show commands.
_EXEC_SHOW_FORBIDDEN_PREFIXES = (
    "ping",
    "traceroute",
    "tracert",
    "attach",
    "run ",
    "bash",
)


def _exec_show_command_forbidden(cmd: str) -> bool:
    text = (cmd or "").strip().lower()
    if not text:
        return False
    for prefix in _EXEC_SHOW_FORBIDDEN_PREFIXES:
        if text == prefix.rstrip() or text.startswith(prefix):
            return True
    return False


def _service_pair_present(args: dict[str, Any]) -> bool:
    st = args.get("service_type") or args.get("type")
    sn = (
        args.get("service_name")
        or args.get("name")
        or args.get("service_id")
        or args.get("id")
    )
    return bool(
        isinstance(st, str)
        and st.strip()
        and isinstance(sn, str)
        and sn.strip()
    )


def task_rejection_reason(
    check: str,
    args: dict[str, Any],
    *,
    allowlist: frozenset[str],
    device_names: set[str],
    force_device: str | None = None,
) -> str | None:
    """Return why a deep-check task is rejected, or None if acceptable."""
    if not isinstance(check, str) or check not in allowlist:
        return f"tool {check!r} not on allowlist {sorted(allowlist)}"

    device = args.get("device") or args.get("device_name")
    if force_device:
        device = force_device
    if device is not None:
        if not isinstance(device, str) or not device.strip():
            return "device_name must be a non-empty string"
        if device not in device_names:
            return f"device {device!r} not in known_devices"
        try:
            from nso_facts.mcp_client import is_device_quarantined

            if is_device_quarantined(device):
                return (
                    f"device {device!r} is quarantined this run "
                    "(live MCP unavailable — do not retry; use another endpoint "
                    "or conclude with uncertainty)"
                )
        except Exception:  # noqa: BLE001 — quarantine optional outside MCP ctx
            pass

    if check == "exec_show":
        cmd = args.get("command") or args.get("input_command")
        if not isinstance(cmd, str) or not cmd.strip():
            return "exec_show requires input_command (CLI without leading 'show')"
        if _exec_show_command_forbidden(cmd):
            return (
                f"exec_show cannot run {cmd!r} (becomes 'show …'). "
                "Ping/traceroute are not show commands — omit reachability via "
                "exec_show; do not retry."
            )
        if not (force_device or device):
            return "exec_show requires device_name"

    if check in ("get_hardware_health", "get_interface_health"):
        if not (force_device or device):
            return f"{check} requires device_name"

    if check in ("check_service_sync", "compare_service_config"):
        if not _service_pair_present(args):
            return (
                f"{check} requires service_type + service_name "
                "(never device_name alone)"
            )

    return None


def dedupe_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop exact duplicate issues (same code/message/edge_id), keep order."""
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for issue in issues:
        key = (
            issue.get("code"),
            issue.get("message"),
            issue.get("edge_id"),
            issue.get("layer"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(issue)
    return out


@dataclass
class AgentResult:
    name: str
    layer: str
    static_summary: dict[str, Any] = field(default_factory=dict)
    operational_summary: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, int] = field(default_factory=dict)
    static_edges: list[dict[str, Any]] = field(default_factory=list)
    operational_edges: list[dict[str, Any]] = field(default_factory=list)
    # device agent extras
    seed: dict[str, Any] = field(default_factory=dict)
    hardware: dict[str, Any] = field(default_factory=dict)
    narrative: str = ""

    def fact_pack(self) -> dict[str, Any]:
        pack: dict[str, Any] = {
            "agent": self.name,
            "layer": self.layer,
            "static_summary": self.static_summary,
            "operational_summary": self.operational_summary,
            "issues": self.issues,
            "evidence": self.evidence,
            "plan": self.plan,
            "coverage": self.coverage,
        }
        if self.static_edges:
            pack["static_edges"] = self.static_edges
        if self.operational_edges:
            pack["operational_edges"] = self.operational_edges
        if self.seed:
            pack["seed"] = self.seed
        if self.hardware:
            pack["hardware"] = self.hardware
        if self.narrative:
            pack["narrative"] = self.narrative
        return pack


def parse_plan_json(text: str) -> list[dict[str, Any]]:
    """Extract a JSON task array from model output."""
    raw = (text or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = _JSON_ARRAY_RE.search(raw)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def gate_plan(
    tasks: list[dict[str, Any]],
    *,
    allowlist: frozenset[str],
    device_names: set[str],
    max_tasks: int = _MAX_TASKS_DEFAULT,
    force_device: str | None = None,
) -> list[dict[str, Any]]:
    """Keep only schema-valid, allowlisted tasks (budget capped).

    If ``force_device`` is set, every task is pinned to that device (wrong
    device names are rewritten or rejected for tools that need a device).
    Rejects quarantined devices, ping/traceroute via exec_show, and
    check_service_sync without service_type + service_name.
    """
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in tasks:
        check = task.get("check")
        if not isinstance(check, str) or check not in allowlist:
            continue
        args = task.get("args")
        if not isinstance(args, dict):
            args = {}
        else:
            args = dict(args)

        if force_device:
            args["device"] = force_device
            if "device_name" in args:
                args["device_name"] = force_device

        if task_rejection_reason(
            check,
            args,
            allowlist=allowlist,
            device_names=device_names,
            force_device=force_device,
        ):
            continue

        if force_device and check in (
            "get_hardware_health",
            "get_interface_health",
        ):
            args = {"device_name": force_device}

        key = json.dumps({"check": check, "args": args}, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        accepted.append(
            {
                "check": check,
                "args": args,
                "reason": task.get("reason"),
                "issue_id": task.get("issue_id") or task.get("edge_id"),
            }
        )
        if len(accepted) >= max_tasks:
            break
    return accepted


def format_domain_section(result: AgentResult) -> str:
    """Human-readable ISIS/BGP section with bidirectional issue list."""
    title = "IS-IS connectivity" if result.layer == "underlay" else "BGP peering"
    lines = [f"## {title}", ""]
    op = result.operational_summary or {}
    if result.layer == "underlay":
        lines.append(
            "Adjacencies (bidirectional): "
            f"total={op.get('total', 0)} "
            f"up={op.get('up', 0)} "
            f"down={op.get('down', 0)} "
            f"unidirectional={op.get('unidirectional', 0)} "
            f"unknown={op.get('unknown', 0)}"
        )
    else:
        lines.append(
            "Sessions (bidirectional): "
            f"total={op.get('total', 0)} "
            f"up={op.get('up', 0)} "
            f"down={op.get('down', 0)} "
            f"degraded={op.get('degraded', 0)} "
            f"unknown={op.get('unknown', 0)}"
        )
    lines.append("")
    if not result.issues:
        lines.append("No bidirectional issues detected.")
    else:
        lines.append(f"Issues ({len(result.issues)}):")
        for issue in result.issues:
            sev = issue.get("severity", "?")
            code = issue.get("code", "?")
            msg = issue.get("message", "")
            eid = issue.get("edge_id") or ""
            suffix = f" [{eid}]" if eid else ""
            lines.append(f"- ({sev}) {code}: {msg}{suffix}")
    if result.evidence:
        lines.append("")
        lines.extend(format_evidence_preview(result.evidence))
    lines.append("")
    return "\n".join(lines)


def format_evidence_preview(
    evidence: list[dict[str, Any]],
    *,
    max_entries: int = 8,
) -> list[str]:
    """Compact deep-check summary for the report (glanceable, not raw dumps)."""
    ok_n = sum(
        1
        for e in evidence
        if isinstance(e, dict) and "error" not in e and "result" in e
    )
    err_n = sum(1 for e in evidence if isinstance(e, dict) and e.get("error"))
    lines = [f"Deep-check evidence ({len(evidence)}): ok={ok_n} err={err_n}"]
    for i, entry in enumerate(evidence[:max_entries]):
        if not isinstance(entry, dict):
            continue
        check = str(entry.get("check") or "?")
        args = entry.get("args") if isinstance(entry.get("args"), dict) else {}
        status = "err" if entry.get("error") else "ok"
        headline = _evidence_headline(check, args)
        reason = entry.get("reason")
        reason_s = f"  — {reason}" if reason else ""
        lines.append(f"  [{i + 1}] {status}  {headline}{reason_s}")
        if entry.get("error"):
            lines.append(f"      {_clip(str(entry['error']), 120)}")
            continue
        summary = _evidence_result_summary(check, args, entry.get("result"))
        if summary:
            lines.append(f"      {summary}")
    if len(evidence) > max_entries:
        lines.append(f"  … {len(evidence) - max_entries} more in run artifacts")
    return lines


def _evidence_headline(check: str, args: dict[str, Any]) -> str:
    dev = args.get("device_name") or args.get("device") or ""
    if check == "exec_show":
        cmd = args.get("input_command") or args.get("command") or ""
        return f"{dev}  exec_show {cmd!r}".strip()
    if dev:
        return f"{dev}  {check}"
    return check


def _evidence_result_summary(check: str, args: dict[str, Any], result: Any) -> str:
    text = _extract_show_text(result)
    cmd = str(args.get("input_command") or args.get("command") or "").lower()
    if check == "exec_show" and text:
        if "bgp" in cmd and "summary" in cmd:
            return _summarize_bgp_summary(text)
        if "isis" in cmd and ("neighbor" in cmd or "adjacency" in cmd):
            return _summarize_isis_neighbors(text)
        if "isis" in cmd and "interface" in cmd:
            return _summarize_isis_interface_brief(text)
        # Generic: first non-empty content line
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("RP/") and "UTC" not in s:
                return _clip(s, 100)
        return _clip(" ".join(text.split()), 100)
    if check == "verify_bgp_peer_reachability":
        return _clip(_result_preview(result), 120)
    if result is None:
        return ""
    return _clip(_result_preview(result), 100)


def _extract_show_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    if result.get("truncated") and result.get("preview"):
        return str(result["preview"])
    data = result.get("data")
    if isinstance(data, dict):
        inner = data.get("result")
        if isinstance(inner, str):
            return inner
        if inner is not None:
            return str(inner)
    for key in ("result", "output", "text"):
        val = result.get(key)
        if isinstance(val, str):
            return val
    return ""


def _summarize_bgp_summary(text: str) -> str:
    """One-line neighbor rollup from show bgp summary."""
    peers: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        addr = parts[0]
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", addr) and ":" not in addr:
            continue
        # Last field is state word or prefix count
        last = parts[-1]
        if last.isdigit():
            n = int(last)
            pfx = f"{n // 1000}k" if n >= 1000 else str(n)
            peers.append(f"{addr}=Est/{pfx}")
        else:
            peers.append(f"{addr}={last}")
    if not peers:
        return "bgp summary ok (no neighbor rows parsed)"
    return "peers: " + " · ".join(peers[:8]) + (
        f" · +{len(peers) - 8} more" if len(peers) > 8 else ""
    )


def _summarize_isis_neighbors(text: str) -> str:
    """One-line adjacency rollup from isis neighbors / adjacency."""
    rows: list[str] = []
    skip_sys = {
        "system",
        "total",
        "is-is",
        "interface",
        "ok",
        "tue",
        "wed",
        "thu",
        "fri",
        "sat",
        "sun",
        "mon",
        "aug",
        "jan",
        "feb",
        "mar",
        "apr",
        "may",
        "jun",
        "jul",
        "sep",
        "oct",
        "nov",
        "dec",
    }
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        sys_id, iface = parts[0], parts[1]
        if sys_id.lower().rstrip(":") in skip_sys:
            continue
        if not _looks_like_xr_iface(iface):
            continue
        state = next((p for p in parts if p in {"Up", "Down"}), None)
        if state is None:
            continue
        rows.append(f"{sys_id}@{iface}={state}")
    if not rows:
        return "isis neighbors ok (no adjacency rows parsed)"
    return "adj: " + " · ".join(rows[:8]) + (
        f" · +{len(rows) - 8} more" if len(rows) > 8 else ""
    )


def _summarize_isis_interface_brief(text: str) -> str:
    """Compact rollup from isis interface brief."""
    up = down = 0
    ifaces: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or not _looks_like_xr_iface(parts[0]):
            continue
        iface = parts[0]
        # CLNS column often near end before MTU/prio; prefer explicit Up/Down
        state = next((p for p in parts[1:] if p in {"Up", "Down"}), None)
        if state == "Up":
            up += 1
        elif state == "Down":
            down += 1
        ifaces.append(iface)
    if not ifaces:
        return "isis interface brief ok (no interface rows parsed)"
    shown = ", ".join(ifaces[:6])
    extra = f" (+{len(ifaces) - 6})" if len(ifaces) > 6 else ""
    return f"ifaces {len(ifaces)} (CLNS up={up} down={down}): {shown}{extra}"


def _looks_like_xr_iface(name: str) -> bool:
    n = name.strip()
    if "/" in n:
        return True
    return bool(
        re.match(
            r"^(Hu|Fo|Te|Gi|TF|FH|BV|BE|Mg|Nu|Bundle-Ether|HundredGigE|"
            r"FortyGigE|TenGigE|FourHundredGigE|BVI)\S*",
            n,
            re.I,
        )
    )


def _result_preview(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if result.get("truncated") and result.get("preview"):
            return str(result["preview"])
        for key in ("data", "output", "text", "error_message"):
            if key in result and result[key] is not None:
                return str(result[key])
        return json.dumps(result, default=str)
    return str(result)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
