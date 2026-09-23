"""Per-run MCP call accounting: count and classify NSO vs device tools."""

from __future__ import annotations

import sys
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

# Live / southbound — typically hits the device (via NSO NED / live-status).
_DEVICE_TOOLS = frozenset(
    {
        "exec_show",
        "exec_ping",
        "exec_traceroute",
        "get_live_status",
        "get_interface_health",
        "get_hardware_health",
        "get_device_state",
        "check_isis_adjacencies",
        "check_telemetry_baseline",
        "check_device_sync",
        "sync_from_device",
        "verify_collector_reachability",
        "verify_ntp_reachability",
        "verify_bgp_peer_reachability",
        "verify_dns_reachability",
    }
)

# Northbound NSO / CDB / service inventory (config in NSO, not a live show).
_NSO_TOOLS = frozenset(
    {
        "get_service_types",
        "get_services",
        "check_service_sync",
        "compare_service_config",
        "get_device_config",
        "compare_device_config",
        "list_devices",
        "get_device_groups",
        "get_device_platform",
        "get_device_ned_ids",
        "get_device_xr_variant",
        "get_nso_capabilities",
        "explore_nso_path",
        "get_fleet_sync_summary",
        "get_compliance_report",
        "get_rollback_list",
        "get_rollback",
    }
)

# Preferred order for stage summary lines (unknown stages follow alphabetically).
_STAGE_ORDER = (
    "focus",
    "spines",
    "dataplane",
    "autonomous",
    "drill",
)

_Kind = str  # "nso" | "device" | "other"

_active: ContextVar[list["McpCallRecord"] | None] = ContextVar(
    "mcp_call_records", default=None
)
_stage: ContextVar[str | None] = ContextVar("mcp_call_stage", default=None)


@dataclass
class McpCallRecord:
    seq: int
    tool: str
    kind: _Kind
    device: str | None = None
    detail: str | None = None
    stage: str | None = None
    cached: bool = False


@dataclass
class McpCallStats:
    records: list[McpCallRecord] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.records)

    @property
    def cache_hits(self) -> int:
        return sum(1 for r in self.records if r.cached)

    @property
    def wire(self) -> int:
        return self.total - self.cache_hits

    def count(self, kind: _Kind) -> int:
        return sum(1 for r in self.records if r.kind == kind)

    def wire_count(self, kind: _Kind) -> int:
        """Wire (non-cache) calls of one kind."""
        return sum(1 for r in self.records if r.kind == kind and not r.cached)

    @property
    def wired_nso(self) -> int:
        return self.wire_count("nso")

    @property
    def wired_device(self) -> int:
        return self.wire_count("device")

    @property
    def wired_other(self) -> int:
        return self.wire_count("other")

    def stage_counts(self) -> dict[str, int]:
        """Counts by stage label; untagged calls use ``other``."""
        out: dict[str, int] = {}
        for r in self.records:
            key = r.stage or "other"
            out[key] = out.get(key, 0) + 1
        return out


def classify_mcp_tool(tool: str) -> _Kind:
    name = (tool or "").strip()
    if name in _DEVICE_TOOLS:
        return "device"
    if name in _NSO_TOOLS:
        return "nso"
    return "other"


def _device_from_params(params: dict[str, Any] | None) -> str | None:
    if not isinstance(params, dict):
        return None
    for key in ("device_name", "device", "device_id"):
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _detail_from_params(tool: str, params: dict[str, Any] | None) -> str | None:
    if not isinstance(params, dict):
        return None
    if tool == "exec_show":
        cmd = params.get("input_command") or params.get("command")
        if isinstance(cmd, str) and cmd.strip():
            return cmd.strip()[:80]
    if tool in {"explore_nso_path", "get_rollback"}:
        path = params.get("path")
        if isinstance(path, str) and path.strip():
            return path.strip()[:80]
    if tool in {"get_services", "check_service_sync", "compare_service_config"}:
        st = params.get("service_type")
        sn = params.get("service_name") or params.get("name")
        bits = [str(x) for x in (st, sn) if isinstance(x, str) and x.strip()]
        return " ".join(bits)[:80] if bits else None
    return None


def start_mcp_accounting() -> None:
    """Begin recording MCP calls for this run (clears prior records)."""
    _active.set([])
    _stage.set(None)


def stop_mcp_accounting() -> McpCallStats:
    """Stop recording and return stats (empty if never started)."""
    records = _active.get()
    _active.set(None)
    _stage.set(None)
    return McpCallStats(list(records or []))


def set_mcp_stage(stage: str | None) -> None:
    """Label subsequent ``record_mcp_call`` entries (e.g. spines, dataplane)."""
    if stage is None:
        _stage.set(None)
        return
    name = str(stage).strip()
    _stage.set(name or None)


def record_mcp_call(
    tool: str,
    params: dict[str, Any] | None = None,
    *,
    cached: bool = False,
) -> None:
    """Append one call if accounting is active."""
    records = _active.get()
    if records is None:
        return
    kind = classify_mcp_tool(tool)
    records.append(
        McpCallRecord(
            seq=len(records) + 1,
            tool=str(tool or ""),
            kind=kind,
            device=_device_from_params(params),
            detail=_detail_from_params(str(tool or ""), params),
            stage=_stage.get(),
            cached=bool(cached),
        )
    )


def _format_stage_summary(stats: McpCallStats) -> str | None:
    counts = stats.stage_counts()
    if not counts:
        return None
    ordered: list[str] = []
    seen: set[str] = set()
    for name in _STAGE_ORDER:
        if name in counts:
            ordered.append(f"{name}={counts[name]}")
            seen.add(name)
    for name in sorted(k for k in counts if k not in seen):
        ordered.append(f"{name}={counts[name]}")
    return "[mcp] stage " + " ".join(ordered)


def format_mcp_accounting(stats: McpCallStats) -> list[str]:
    """Human-readable summary + per-call lines for stderr."""
    lines = [
        f"[mcp] total={stats.total} "
        f"wire={stats.wire} "
        f"cache_hits={stats.cache_hits} "
        f"wired_nso={stats.wired_nso} "
        f"wired_device={stats.wired_device} "
        f"nso={stats.count('nso')} "
        f"device={stats.count('device')} "
        f"other={stats.count('other')}"
    ]
    stage_line = _format_stage_summary(stats)
    if stage_line:
        lines.append(stage_line)
    for r in stats.records:
        bits = [f"[mcp] {r.seq}"]
        if r.stage:
            bits.append(r.stage)
        bits.extend([r.kind, r.tool])
        if r.cached:
            bits.append("cache_hit")
        if r.device:
            bits.append(f"device={r.device}")
        if r.detail:
            bits.append(r.detail)
        lines.append(" ".join(bits))
    return lines


def print_mcp_accounting(stats: McpCallStats, *, file: Any = None) -> None:
    out = file if file is not None else sys.stderr
    for line in format_mcp_accounting(stats):
        print(line, file=out)
