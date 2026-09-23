"""LLM dataplane verify for services with system=up and dataplane=not_checked."""

from __future__ import annotations

from diagnostic_mas.investigation import GAP_SCHEMA, Investigation, normalize_gap, fallback_gap

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from agent.config import Settings
from agent.summarize import FABRIC_CHAT_TIMEOUT_SEC
from diagnostic_mas.case import Budget, CaseFile, DrillSession, add_evidence
from diagnostic_mas.deep_checks import DATAPLANE_ALLOWLIST
from diagnostic_mas.drill import (
    _message_to_dict,
    execute_one_drill_call,
)
from diagnostic_mas.tool_context import BatchProgress, result_error, tool_catalog
from diagnostic_mas.focus import counts_from_services
from diagnostic_mas.roles.service import issues_from_service_health
from nso_facts.health import apply_dataplane_status, combine_service_status

_PROMPTS = Path(__file__).resolve().parent / "prompts"
_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")

# Keep LLM context small — full device configs blow Fabric's chat timeout.
_DATAPLANE_RESULT_CLIP = 8_000
_CONFIG_KEEP_RE = re.compile(
    r"(?i)(route[-_ ]?target|\bevi\b|bridge-domain|bridge group|l2vpn|\bevpn\b|"
    r"xconnect|pseudowire|vfi\b|bd-|bg-)",
)

DATAPLANE_MCP_CALL_TOOL = {
    "type": "function",
    "function": {
        "name": "mcp_call",
        "description": (
            "Call a read-only NSO MCP tool. tool_name is REQUIRED and must be "
            "one of: "
            + ", ".join(sorted(DATAPLANE_ALLOWLIST))
            + ". Never omit tool_name. For device CLI shows always set "
            "tool_name=exec_show with params.device_name + params.input_command "
            "(no leading 'show'). Other params: service tools need "
            "service_type + service_name; get_interface_health is device-wide "
            "(device_name only); explore_nso_path needs a module-qualified "
            "RESTCONF path (e.g. 'tailf-ncs:devices/device=<name>'); never "
            "'/services/…' — prefer get_services / check_service_sync for "
            "service instances. Do not call tools against "
            "quarantined/unavailable devices. Batch several in one turn "
            "when useful."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": (
                        "Exact allowlisted MCP tool name (e.g. exec_show). "
                        "Required — do not leave blank."
                    ),
                },
                "params": {"type": "object"},
                "reason": {
                    "type": "string",
                    "description": "Short why this check matters",
                },
            },
            "required": ["tool_name"],
        },
    },
}

CONCLUDE_DATAPLANE_TOOL = {
    "type": "function",
    "function": {
        "name": "conclude_dataplane",
        "description": (
            "End dataplane verification with dataplane_status and grounded "
            "observed/cause. Call after evaluating the latest tool batch. "
            "dataplane_status=up requires positive PE-side forwarding "
            "readiness (not merely no fault found). Do not infer idle "
            "customer or fault outside managed scope; keep traffic delivery "
            "unverified until evidence resolves it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dataplane_status": {
                    "type": "string",
                    "enum": ["up", "down", "degraded", "unknown"],
                },
                "observed": {"type": "string"},
                "cause": {"type": "string"},
                "fix_suggestion": {"type": "string"},
                "verification_gap": GAP_SCHEMA,
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                },
            },
            "required": ["dataplane_status", "observed", "cause"],
        },
    },
}

DATAPLANE_TOOLS = [DATAPLANE_MCP_CALL_TOOL, CONCLUDE_DATAPLANE_TOOL]

# Dataplane verify tool budget (overridable via --max-dataplane-tools).
_DATAPLANE_MAX_TOOLS_DEFAULT = 40
# Round cap scales with tools; hard ceiling keeps runaway loops in check.
_DATAPLANE_MAX_ROUNDS_CAP = 30
_SOFT_LIVE_L2_ERRORS = frozenset({"ac_not_found", "no_xconnect_data"})


def dataplane_tools_cap(budget: Budget | Any) -> int:
    """Per-service MCP tool budget. ``0`` disables verify; never coerce 0→40."""
    raw = getattr(budget, "max_dataplane_tools", None)
    if raw is None:
        return _DATAPLANE_MAX_TOOLS_DEFAULT
    return max(0, int(raw))


def dataplane_system_prompt(service_type: str | None = None) -> str:
    """Load dataplane_agent_<type>.txt when present; else dataplane_agent.txt."""
    fallback = (
        "Verify dataplane for one service with system=up. "
        "Call conclude_dataplane with dataplane_status."
    )
    stype = str(service_type or "").strip().lower()
    candidates: list[Path] = []
    prompt_stem = typed_dataplane_prompt_stem(stype) if stype else None
    if prompt_stem:
        candidates.append(_PROMPTS / f"dataplane_agent_{prompt_stem}.txt")
    elif stype:
        candidates.append(_PROMPTS / f"dataplane_agent_{stype}.txt")
        # Common aliases (kept for direct path tries before generic)
        if stype in {"l2bridge", "l2-bridge"} or "sts" in stype:
            candidates.append(_PROMPTS / "dataplane_agent_l2sts.txt")
        if stype in {"l2vpws", "vpws", "l2-ptp"}:
            candidates.append(_PROMPTS / "dataplane_agent_l2ptp.txt")
        if stype in {"l3vpn", "l3-rt", "l3routing"}:
            candidates.append(_PROMPTS / "dataplane_agent_l3rt.txt")
    candidates.append(_PROMPTS / "dataplane_agent.txt")
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return fallback


def list_typed_dataplane_prompt_types() -> frozenset[str]:
    """Service-type stems that have ``dataplane_agent_<type>.txt`` on disk.

    Excludes the generic ``dataplane_agent.txt``. New typed prompts are picked
    up automatically without a code change.
    """
    found: set[str] = set()
    if not _PROMPTS.is_dir():
        return frozenset()
    for path in _PROMPTS.glob("dataplane_agent_*.txt"):
        stem = path.name.removeprefix("dataplane_agent_").removesuffix(".txt")
        if stem:
            found.add(stem.lower())
    return frozenset(found)


def typed_dataplane_prompt_stem(service_type: str | None) -> str | None:
    """Map a service_type to a typed prompt stem, or None if none exists."""
    known = list_typed_dataplane_prompt_types()
    stype = str(service_type or "").strip().lower()
    if not stype:
        return None
    if stype in known:
        return stype
    # Aliases → canonical typed prompt when that file exists
    alias_map: list[tuple[tuple[str, ...], str]] = [
        (("l2bridge", "l2-bridge"), "l2sts"),
        (("l2vpws", "vpws", "l2-ptp"), "l2ptp"),
        (("l3vpn", "l3-rt", "l3routing"), "l3rt"),
    ]
    for aliases, canon in alias_map:
        if stype in aliases and canon in known:
            return canon
    if "sts" in stype and "l2sts" in known:
        return "l2sts"
    return None


def _system_prompt(service_type: str | None = None) -> str:
    return dataplane_system_prompt(service_type)


def _log(msg: str) -> None:
    print(f"[dataplane] {msg}", file=sys.stderr)


def _snip_llm_content_for_log(content: Any, *, limit: int = 240) -> str:
    """Single-line preview of assistant content for empty-tool-turn diagnostics."""
    if content is None:
        return ""
    text = str(content).replace("\r", " ").replace("\n", "\\n")
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _empty_tool_turn_log_line(
    message: Any,
    *,
    finish_reason: Any = None,
    limit: int = 240,
) -> str:
    """Describe a no-tool_calls turn for stderr (finish_reason + content snip)."""
    bits: list[str] = []
    fr = finish_reason if finish_reason is not None else getattr(
        message, "finish_reason", None
    )
    if fr is not None and str(fr).strip():
        bits.append(f"finish_reason={fr!s}")
    snip = _snip_llm_content_for_log(getattr(message, "content", None), limit=limit)
    if snip:
        bits.append(f"content={snip!r}")
    else:
        bits.append("content=<empty>")
    return "; ".join(bits)


def _messages_chars(messages: list[dict[str, Any]]) -> int:
    return len(json.dumps(messages, default=str))


def _dump_failed_dataplane_llm_request(
    *,
    service_name: str,
    round_i: int,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    error: BaseException,
    elapsed_s: float,
) -> Path | None:
    """Write the failed chat payload for offline inspection; return path or None.

    Enable with ``DATAPLANE_LLM_DUMP=1`` (cwd file) or ``DATAPLANE_LLM_DUMP=/path``
    (file or directory). Set to ``0``/``false`` to disable (default: off).
    """
    raw = (os.environ.get("DATAPLANE_LLM_DUMP") or "").strip()
    if not raw or raw.lower() in {"0", "false", "no", "off"}:
        return None
    safe = re.sub(r"[^\w.-]+", "_", service_name)[:80] or "service"
    fname = f"dataplane_llm_fail_{safe}_r{round_i}.json"
    if raw.lower() in {"1", "true", "yes", "on"}:
        path = Path(fname)
    else:
        path = Path(raw)
        if path.suffix.lower() != ".json":
            path = path / fname
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        msg_chars = _messages_chars(messages)
        tools_chars = len(json.dumps(tools, default=str))
        path.write_text(
            json.dumps(
                {
                    "service": service_name,
                    "round": round_i,
                    "model": model,
                    "timeout_sec": FABRIC_CHAT_TIMEOUT_SEC,
                    "elapsed_s": round(elapsed_s, 2),
                    "error": f"{type(error).__name__}: {error}",
                    "messages_chars": msg_chars,
                    "tools_chars": tools_chars,
                    "total_chars_approx": msg_chars + tools_chars,
                    "messages": messages,
                    "tools": tools,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        return path
    except OSError as exc:
        _log(f"could not dump failed LLM request: {exc}")
        return None


def _spine_extra(ev: dict[str, Any]) -> dict[str, Any] | None:
    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
    extra = payload.get("extra") if isinstance(payload, dict) else None
    return extra if isinstance(extra, dict) else None


def iter_service_records(case: CaseFile) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return (spine_evidence, service_record) for each instance in spine extra."""
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for ev in case.evidence:
        if ev.get("kind") != "spine" or ev.get("role") != "service":
            continue
        extra = _spine_extra(ev)
        if not extra:
            continue
        services = extra.get("services")
        if not isinstance(services, dict):
            continue
        for _key, rec in services.items():
            if isinstance(rec, dict) and rec.get("name"):
                out.append((ev, rec))
    return out


def _live_l2_clearly_up(record: dict[str, Any]) -> bool:
    live = record.get("live_l2") if isinstance(record.get("live_l2"), dict) else None
    if not isinstance(live, dict):
        return False
    eps = [e for e in (live.get("endpoints") or []) if isinstance(e, dict)]
    if not eps:
        return False
    for ep in eps:
        if ep.get("error"):
            return False
        st = str(ep.get("st") or "").upper()
        if st and st != "UP":
            return False
    return True


def service_needs_investigation(record: dict[str, Any]) -> bool:
    """True when basic checks look suspicious or inconclusive (prefer LLM)."""
    if _live_l2_all_soft_errors(record):
        return True
    status = str(record.get("status") or "").lower()
    if status in {"down", "degraded", "unknown"}:
        return True
    sys = str(record.get("system_status") or "").lower()
    if sys in {"down", "degraded", "unknown"}:
        return True
    live = record.get("live_l2") if isinstance(record.get("live_l2"), dict) else None
    if isinstance(live, dict) and live.get("endpoints"):
        summary = str(live.get("summary") or "").lower()
        if summary in {"down", "degraded", "unknown"}:
            return True
        if not _live_l2_clearly_up(record):
            return True
        return False
    # No live_l2 endpoints: healthy sync-up needs no LLM by default.
    return False


def init_service_coverage(case: CaseFile) -> None:
    """Mark every matched service as basic_passed or needing investigation."""
    for _ev, rec in iter_service_records(case):
        name = str(rec.get("name") or "").strip()
        if not name:
            continue
        if service_needs_investigation(rec):
            case.service_coverage[name] = "needs_investigation"
        else:
            case.service_coverage[name] = "basic_passed"


def _dataplane_priority(rec: dict[str, Any]) -> tuple[int, str]:
    """Lower = verify sooner. Prefer live_l2 failures over healthy L3 sync-up."""
    if _live_l2_all_soft_errors(rec):
        return (0, str(rec.get("name") or ""))
    live = rec.get("live_l2") if isinstance(rec.get("live_l2"), dict) else None
    if isinstance(live, dict):
        summary = str(live.get("summary") or "").lower()
        if summary in {"down", "degraded", "unknown"}:
            return (1, str(rec.get("name") or ""))
        if live.get("endpoints") and not _live_l2_clearly_up(rec):
            return (2, str(rec.get("name") or ""))
    status = str(rec.get("status") or "").lower()
    if status in {"down", "degraded"}:
        return (2, str(rec.get("name") or ""))
    stype = str(rec.get("service_type") or "").lower()
    if stype.startswith("l2"):
        return (3, str(rec.get("name") or ""))
    return (4, str(rec.get("name") or ""))


def _category_rotation_order(
    stems: list[str], *, seed: str | None = None
) -> list[str]:
    """Stable category order with optional seed rotation.

    Alphabetically sorted stems are rotated by a seed offset so the same
    category (e.g. l3rt) is not always last across overnight samples.
    """
    ordered = sorted({str(s) for s in stems if s})
    if len(ordered) <= 1:
        return ordered
    if not seed:
        return ordered
    import hashlib

    digest = hashlib.sha256(str(seed).encode("utf-8")).hexdigest()
    offset = int(digest[:8], 16) % len(ordered)
    return ordered[offset:] + ordered[:offset]


def _pick_round_robin(
    by_cat: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]],
    *,
    per_cat: int,
    order: list[str],
    limit: int | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Interleave categories so one type is not drained before others start."""
    if per_cat <= 0 or not order:
        return []
    idxs = {stem: 0 for stem in order}
    picked: list[tuple[dict[str, Any], dict[str, Any]]] = []
    while True:
        progressed = False
        for stem in order:
            if limit is not None and len(picked) >= limit:
                return picked
            bucket = by_cat.get(stem) or []
            i = idxs[stem]
            if i >= per_cat or i >= len(bucket):
                continue
            picked.append(bucket[i])
            idxs[stem] = i + 1
            progressed = True
        if not progressed:
            break
    return picked


def select_dataplane_candidates(
    case: CaseFile,
    *,
    limit: int | None = None,
    per_category: int | None = None,
    suspicious_only: bool = False,
    explicit_service: bool = False,
    one_per_typed_category: bool = True,
    category_rotate_seed: str | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pick services for LLM dataplane verify.

    Default: ``system=up`` and ``dataplane=not_checked`` (fleet runs).
    ``suspicious_only`` (service focus): only instances that look wrong or
    inconclusive. An explicit service selection also includes basic passes.

    Default selection (``one_per_typed_category=True``, ``limit`` and
    ``per_category`` both ``None``): one best instance per service type that
    has ``dataplane_agent_<type>.txt``, including instances whose basic/live_l2
    checks look fine (``basic_passed``). If only one typed category is present,
    take up to two instances of it.

    ``per_category=N`` takes up to N priority-sorted instances from each typed
    category (``--max-dataplane-per-category``), interleaved round-robin so one
    type is not always last. Optional ``limit`` truncates the combined list.
    ``category_rotate_seed`` rotates which category starts the round-robin.
    """
    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for ev, rec in iter_service_records(case):
        sys = str(rec.get("system_status") or "").lower()
        dp = str(rec.get("dataplane_status") or "not_checked").lower()
        status = str(rec.get("status") or "").lower()
        if suspicious_only:
            if not explicit_service and not service_needs_investigation(rec):
                continue
            # Already concluded dataplane → skip re-verify
            if dp not in {"", "not_checked"} and status not in {"down", "degraded"}:
                if dp in {"up", "ok"} and not _live_l2_all_soft_errors(rec):
                    continue
            eligible.append((ev, rec))
            continue
        # Fleet / one-per-category: basic_passed does NOT exempt a typed type.
        # Soft-error instances stay eligible so priority can prefer them in-cap.
        if sys == "up" and dp in {"", "not_checked"}:
            eligible.append((ev, rec))
    if not eligible:
        return []

    rest = list(eligible)
    rest.sort(key=lambda pair: _dataplane_priority(pair[1]))

    if not one_per_typed_category:
        cap = 0 if limit is None else max(0, int(limit))
        return rest[:cap] if cap > 0 else []

    by_cat: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    untyped: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for ev, rec in rest:
        stem = typed_dataplane_prompt_stem(str(rec.get("service_type") or ""))
        if stem is None:
            untyped.append((ev, rec))
        else:
            by_cat.setdefault(stem, []).append((ev, rec))

    order = _category_rotation_order(
        list(by_cat.keys()), seed=category_rotate_seed
    )

    # Explicit per-category budget (overnight / sample runs).
    if per_category is not None:
        per_cat = max(0, int(per_category))
        picked = _pick_round_robin(
            by_cat,
            per_cat=per_cat,
            order=order,
            limit=None if limit is None else max(0, int(limit)),
        )
        return picked

    # Default: one best per typed category (two if only one category).
    if limit is None:
        per_cat = 2 if len(by_cat) == 1 else 1
        return _pick_round_robin(by_cat, per_cat=per_cat, order=order)

    # Total cap only: round-robin fill, then untyped.
    cap = max(0, int(limit))
    if cap <= 0:
        return []
    # Generous per_cat so round-robin can fill until total cap.
    max_bucket = max((len(v) for v in by_cat.values()), default=0)
    picked = _pick_round_robin(
        by_cat, per_cat=max(max_bucket, 1), order=order, limit=cap
    )
    if len(picked) >= cap:
        return picked
    for ev, rec in untyped:
        if len(picked) >= cap:
            break
        picked.append((ev, rec))
    return picked


def apply_coverage_after_candidate_select(
    case: CaseFile,
    candidates: list[tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    """Update coverage after dataplane candidate selection.

    - ``needs_investigation`` not selected because collection already reported
      the instance unhealthy (not dig-eligible) → ``collection_concluded``
    - ``needs_investigation`` peer of a selected typed category (incl. soft-error
      peers beyond the one-per-category sample) → ``category_peer_skipped``
    - ``needs_investigation`` otherwise not selected → ``budget_skipped``
    - Typed-prompt ``basic_passed`` peers not selected → ``category_peer_skipped``
    """
    selected = {
        str(rec.get("name") or "").strip()
        for _ev, rec in candidates
        if rec.get("name")
    }
    selected_types = {
        typed_dataplane_prompt_stem(str(rec.get("service_type") or ""))
        for _ev, rec in candidates
    }
    selected_types.discard(None)
    for _ev, rec in iter_service_records(case):
        name = str(rec.get("name") or "").strip()
        if not name or name in selected:
            continue
        cov = case.service_coverage.get(name)
        stem = typed_dataplane_prompt_stem(str(rec.get("service_type") or ""))
        if cov == "needs_investigation":
            if _collection_already_unhealthy(rec):
                case.service_coverage[name] = "collection_concluded"
            elif stem is not None and stem in selected_types:
                case.service_coverage[name] = "category_peer_skipped"
            else:
                case.service_coverage[name] = "budget_skipped"
            continue
        if (
            cov == "basic_passed"
            and stem is not None
            and stem in selected_types
        ):
            case.service_coverage[name] = "category_peer_skipped"


def _collection_already_unhealthy(rec: dict[str, Any]) -> bool:
    """True when fleet collection already reported the instance not healthy.

    Dataplane dig selection requires ``system_status=up``; unhealthy / unknown
    collection results (incl. sync timeouts) are not a dig-budget miss.
    """
    status = str(rec.get("status") or "").lower()
    if status in {"down", "degraded", "unknown"}:
        return True
    sys = str(rec.get("system_status") or "").lower()
    if sys in {"down", "degraded", "unknown"}:
        return True
    return False


def update_coverage_from_diagnoses(case: CaseFile) -> None:
    """Refresh coverage from dataplane diagnoses after verify."""
    for dx in case.diagnoses:
        if dx.get("kind") != "dataplane":
            continue
        subject = dx.get("subject") if isinstance(dx.get("subject"), dict) else {}
        name = str(subject.get("name") or dx.get("name") or "").strip()
        if not name:
            continue
        if dx.get("complete") is False:
            case.service_coverage[name] = "unresolved"
        else:
            case.service_coverage[name] = "investigated"


def _live_l2_all_soft_errors(record: dict[str, Any]) -> bool:
    live = record.get("live_l2") if isinstance(record.get("live_l2"), dict) else None
    if not isinstance(live, dict):
        return False
    eps = [e for e in (live.get("endpoints") or []) if isinstance(e, dict)]
    if not eps:
        return False
    return all(e.get("error") in _SOFT_LIVE_L2_ERRORS for e in eps)


def fallback_dataplane_status(record: dict[str, Any]) -> str:
    """Heuristic only — do not use as concluded status when the LLM failed.

    Soft-error live_l2 (``ac_not_found`` / ``no_xconnect_data``) → unknown
    (insufficient evidence). Explicit XC ``DN`` on required endpoints can
    indicate down/degraded; incomplete verify must still prefer unknown.
    """
    if _live_l2_all_soft_errors(record):
        return "unknown"
    live = record.get("live_l2") if isinstance(record.get("live_l2"), dict) else None
    if isinstance(live, dict):
        eps = [e for e in (live.get("endpoints") or []) if isinstance(e, dict)]
        sts = [str(e.get("st") or "").upper() for e in eps if e.get("st")]
        if sts and all(s == "DN" for s in sts):
            return "down"
        if "DN" in sts and "UP" in sts:
            return "degraded"
    return "unknown"


def _incomplete_verify_cause(stop_reason: str | None = None) -> str:
    """Operator-facing incomplete-dig cause; no live_l2 heuristic as status."""
    reason = (stop_reason or "").strip()
    lower = reason.lower()
    if "timed out" in lower or reason.endswith("TimeoutError"):
        why = "the LLM request timed out"
    elif reason.startswith("LLM request failed:"):
        err = reason.split(":", 1)[1].strip() or "error"
        # TimeoutError is reported as timed out (not a generic request failure).
        if "timeout" in err.lower():
            why = "the LLM request timed out"
        else:
            why = f"the LLM request failed ({err})"
    elif "no tools" in lower and "no conclusion" in lower:
        why = (
            "the LLM returned neither tool calls nor a conclude_dataplane "
            "result"
        )
    elif "did not conclude after no-progress" in lower:
        why = "the model did not call conclude_dataplane after a no-progress stop"
    elif "two batches without new successful" in lower:
        why = "two tool batches produced no new successful output"
    elif "budget" in lower and ("exhaust" in lower or "nearly" in lower):
        why = "the dataplane tool budget was exhausted without a conclusion"
    elif reason and reason != "investigation ended without a conclusion":
        why = reason.rstrip(".")
    else:
        why = "verification ended without conclude_dataplane"
    return (
        f"Dataplane verification incomplete because {why}. "
        "Service forwarding status remains unknown."
    )


def _incomplete_verify_observed(stop_reason: str | None = None) -> str:
    """Short Observed line; must not lump every incomplete dig as a timeout."""
    reason = (stop_reason or "").strip()
    lower = reason.lower()
    if (
        "timed out" in lower
        or reason.endswith("TimeoutError")
        or (
            reason.startswith("LLM request failed:")
            and "timeout" in reason.lower()
        )
    ):
        return "LLM did not conclude (chat request timed out)"
    if reason.startswith("LLM request failed:"):
        return "LLM did not conclude (request failed)"
    if "no tools" in lower and "no conclusion" in lower:
        return (
            "LLM did not conclude (returned neither tools nor conclude_dataplane)"
        )
    if "did not conclude after no-progress" in lower:
        return "LLM did not conclude (no-progress stop without conclude_dataplane)"
    if "budget" in lower and ("exhaust" in lower or "nearly" in lower):
        return "LLM did not conclude (dataplane tool budget exhausted)"
    if reason and reason != "investigation ended without a conclusion":
        return f"LLM did not conclude ({reason.rstrip('.')})"
    return "LLM did not conclude (ended without conclude_dataplane)"


def _incomplete_verify_finding(
    record: dict[str, Any],
    *,
    stop_reason: str | None = None,
) -> dict[str, Any]:
    """When LLM times out / never concludes: status=unknown; plain cause text."""
    _ = record  # reserved for future grounded observations without status claim
    return {
        "dataplane_status": "unknown",
        "observed": _incomplete_verify_observed(stop_reason),
        "cause": _incomplete_verify_cause(stop_reason),
        "fix_suggestion": None,
        "confidence": "low",
    }


_NECESSARY_CHECKS = frozenset(
    {"get_device_config", "get_interface_health", "exec_show"}
)


def _service_devices(record: dict[str, Any]) -> list[str]:
    devices = record.get("devices")
    if isinstance(devices, list) and devices:
        return [str(d).strip() for d in devices if str(d).strip()]
    live = record.get("live_l2") if isinstance(record.get("live_l2"), dict) else {}
    out: list[str] = []
    for ep in live.get("endpoints") or []:
        if not isinstance(ep, dict):
            continue
        d = ep.get("device")
        if isinstance(d, str) and d.strip() and d.strip() not in out:
            out.append(d.strip())
    return out


def _device_from_args(args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    for key in ("device", "device_name", "name"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _payload_result_text(payload: dict[str, Any]) -> str:
    if payload.get("error"):
        return ""
    result = payload.get("result")
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)
    except TypeError:
        return str(result)


def _session_by_device(
    session_evidence: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    by_dev: dict[str, list[dict[str, Any]]] = {}
    for ev in session_evidence:
        if ev.get("kind") != "drill":
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        args = payload.get("args")
        device = _device_from_args(args)
        if not device:
            continue
        by_dev.setdefault(device, []).append(ev)
    return by_dev


def _configish_text_for_device(
    session_evidence: list[dict[str, Any]], device: str
) -> str:
    chunks: list[str] = []
    for ev in session_evidence:
        if ev.get("kind") != "drill":
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if _device_from_args(payload.get("args")) != device:
            continue
        check = str(payload.get("check") or "")
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        cmd = str(
            args.get("input_command") or args.get("command") or ""
        ).lower()
        useful = check == "get_device_config" or (
            check == "exec_show"
            and any(tok in cmd for tok in ("evpn", "l2vpn", "route-target", "run"))
        )
        if not useful:
            continue
        text = _payload_result_text(payload)
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def _normalize_show_command(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    cmd = str(args.get("input_command") or args.get("command") or "").strip().lower()
    cmd = re.sub(r"\s+", " ", cmd)
    cmd = re.sub(r"^show\s+", "", cmd)
    return cmd.strip()


def _probe_identity(
    check: str, args: Any, device: str
) -> tuple[str, str, str]:
    """One logical probe: tool + device + distinguishing args (not just tool).

    ``exec_show`` commands differ by command text — a successful unrelated
    show must not clear a failed xconnect (or other) show on the same device.
    """
    if check == "exec_show":
        return (check, device, _normalize_show_command(args))
    args_d = args if isinstance(args, dict) else {}
    # Keep non-show tools device-scoped; optional path/name distinguishes variants.
    extra_bits: list[str] = []
    for key in ("path", "service_name", "service_type", "name"):
        val = args_d.get(key)
        if isinstance(val, str) and val.strip():
            extra_bits.append(f"{key}={val.strip().lower()}")
    return (check, device, "|".join(extra_bits))


def _necessary_check_failed(
    session_evidence: list[dict[str, Any]], devices: list[str]
) -> str | None:
    """If a required probe errored and has no successful retry of *that* probe."""
    want = set(devices)
    failed: dict[tuple[str, str, str], str] = {}
    ok: set[tuple[str, str, str]] = set()
    for ev in session_evidence:
        if ev.get("kind") != "drill":
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        check = str(payload.get("check") or "")
        if check not in _NECESSARY_CHECKS:
            continue
        device = _device_from_args(payload.get("args"))
        if not device or device not in want:
            continue
        key = _probe_identity(check, payload.get("args"), device)
        if payload.get("error"):
            failed[key] = str(payload.get("error"))
        elif _payload_result_text(payload):
            ok.add(key)
    for key, err in failed.items():
        if key not in ok:
            check, device, detail = key
            label = f"{check} {detail!r}".strip() if detail else check
            return f"{label} failed on {device}: {err}"
    return None


def _demote_up_finding(
    finding: dict[str, Any],
    *,
    status: str,
    gate_reason: str,
) -> dict[str, Any]:
    out = dict(finding)
    llm_cause = str(finding.get("cause") or "").strip()
    out["dataplane_status"] = status
    out["confidence"] = "low"
    out["fix_suggestion"] = None
    out["cause"] = (
        f"[gate] {gate_reason}"
        + (f" (LLM claimed up: {llm_cause})" if llm_cause else "")
    )
    observed = str(finding.get("observed") or "").strip()
    note = f"Positive result rejected by verification gate: {gate_reason}"
    out["observed"] = f"{observed}\n{note}".strip() if observed else note
    # unknown = refused "up" for lack of evidence (unresolved — not explained).
    # down/degraded = deterministic fault from evidence (complete diagnosis).
    out["complete"] = status not in {"unknown", "not_checked"}
    return out


def _commit_dataplane_conclusion(
    case: CaseFile,
    record: dict[str, Any],
    finding: dict[str, Any],
    *,
    source: str,
    evidence_ids: list[str] | None = None,
) -> None:
    """Record conclusion; ``unknown`` stays incomplete / unexplained.

    Covers gate-rejected ``up`` claims and LLM ``conclude_dataplane`` with
    ``dataplane_status=unknown`` — neither explains the issue or blocks follow-up.
    """
    status = str(finding.get("dataplane_status") or "").lower()
    if finding.get("complete") is False or status in {"unknown", "not_checked"}:
        _record_incomplete_dataplane_verify(
            case,
            record,
            finding,
            evidence_ids=evidence_ids,
            source=source,
        )
        return
    _record_dataplane_finding(
        case,
        record,
        finding,
        source=source,
        evidence_ids=evidence_ids,
    )


def accept_dataplane_conclusion(
    record: dict[str, Any],
    finding: dict[str, Any],
    *,
    session_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deterministic gates before accepting a positive (up) dataplane result.

    LLM may choose probes and interpret unfamiliar output; code refuses ``up``
    when known requirements are unmet (endpoint coverage, failed checks,
    live_l2 for VPWS, both-PE config evidence for l2sts). RT compatibility
    is dig-reasoned, not auto-diagnosed here. Non-up conclusions pass
    through unchanged.
    """
    out = dict(finding)
    session_evidence = list(session_evidence or [])
    stype = str(record.get("service_type") or "").strip().lower()

    # Wording hygiene for all statuses: never invent counter-clear explanations.
    if stype in {"l2ptp", "l2vpws", "vpws", "l2-ptp"} or not stype:
        out = _scrub_unverified_counter_hypotheses(out)
    if stype == "l2sts":
        out = _scrub_l2sts_pseudoport_flood_overclaim(out)

    if str(out.get("dataplane_status") or "").lower() != "up":
        return out

    # live_l2 is from ``show l2vpn xconnect`` (VPWS). Soft errors / XC ST are
    # decisive for l2ptp, but misleading for l2sts (ACs live in bridge-domain).
    if stype != "l2sts":
        if _live_l2_all_soft_errors(record):
            return _demote_up_finding(
                out,
                status="unknown",
                gate_reason=(
                    "live_l2 ACs missing from xconnect (ac_not_found) — "
                    "insufficient evidence for up or down"
                ),
            )

        hint = fallback_dataplane_status(record)
        if hint in {"down", "degraded"}:
            return _demote_up_finding(
                out,
                status=hint,
                gate_reason=f"live_l2 endpoint status indicates {hint}",
            )

    devices = _service_devices(record)
    by_dev = _session_by_device(session_evidence)
    if len(devices) >= 2:
        missing = [d for d in devices if d not in by_dev]
        if missing:
            return _demote_up_finding(
                out,
                status="unknown",
                gate_reason=(
                    "required endpoints not checked in this session: "
                    + ", ".join(missing)
                ),
            )

    failed = _necessary_check_failed(session_evidence, devices)
    if failed:
        return _demote_up_finding(
            out,
            status="unknown",
            gate_reason=f"necessary check failed — {failed}",
        )

    if stype == "l2sts" and len(devices) >= 2:
        texts = {d: _configish_text_for_device(session_evidence, d) for d in devices}
        if any(not texts[d].strip() for d in devices):
            return _demote_up_finding(
                out,
                status="unknown",
                gate_reason=(
                    "l2sts up requires config-like evidence on both PEs "
                    "in this session (RT compatibility is dig-reasoned, "
                    "not auto-diagnosed)"
                ),
            )
        # Do not auto-compare route-targets here: truncation used to cause
        # false "could not extract RTs" unknowns, and effective RTs belong
        # in service-specific dig reasoning (prompt), not a Python verdict.

        # When the model itself admits incomplete bidirectional MAC/EVPN/RT
        # proof, refuse up — prompt requires unknown for that verification gap.
        admit = _l2sts_up_admits_incomplete_bidirectional_proof(
            f"{out.get('observed') or ''}\n{out.get('cause') or ''}"
        )
        if admit:
            return _demote_up_finding(
                out,
                status="unknown",
                gate_reason=admit,
            )

    return out


_COUNTER_CLEAR_HYPOTHESIS_RE = re.compile(
    r"(?i)(likely|probably|perhaps|may (be|reflect)|possibly|"
    r"consistent with|explained by|due to).{0,80}"
    r"(counter[- ]clear|clear(?:ed|ing)? times?|last[- ]clear|"
    r"different clear|counter history|one-way/idle|idle customer)"
)
_ASYMMETRIC_COUNTERS_RE = re.compile(
    r"(?i)"
    r"(?:(?:counter|pkt|pkts|packet|packets|bytes).{0,120}"
    r"(?:differ|asymmetric|asymmetry|mismatch|unequal)|"
    r"(?:differ|asymmetric|asymmetry).{0,40}(?:counter|pkt|packet)|"
    r"(?:per[- ]?end\s+)?totals?\s+differ)"
)


def _scrub_unverified_counter_hypotheses(finding: dict[str, Any]) -> dict[str, Any]:
    """Replace invented counter-clear / idle explanations with 'reason not established'.

    Keeps dataplane=up when PE-side readiness is otherwise accepted — this is
    wording hygiene, not a status demote. Distinguishes "no fault found" from
    "this possible explanation was proven."
    """
    obs = str(finding.get("observed") or "")
    cause = str(finding.get("cause") or "")
    blob = f"{obs}\n{cause}"
    if not _ASYMMETRIC_COUNTERS_RE.search(blob):
        return finding
    if not _COUNTER_CLEAR_HYPOTHESIS_RE.search(blob):
        return finding

    out = dict(finding)
    replacement = (
        "Packet-counter totals differ; the reason was not established "
        "(missing check: counter-clear / last-clear times on both ACs — "
        "not proven)."
    )
    clause_re = re.compile(
        r"(?i)(?:though |but |and )?(?:per[- ]?end\s+)?totals?\s+differ[,;]?"
        r".{0,140}?(?:counter[- ]clear|clear(?:ed|ing)? times?|last[- ]clear|"
        r"idle customer|one-way/idle).{0,40}"
    )

    def _rewrite(text: str) -> str:
        if not text.strip():
            return text
        if clause_re.search(text):
            return clause_re.sub(replacement, text).strip()
        scrubbed = _COUNTER_CLEAR_HYPOTHESIS_RE.sub(
            "reason not established (counter-clear times not checked)",
            text,
        )
        if "reason not established" not in scrubbed.lower():
            return f"{scrubbed.rstrip()} {replacement}".strip()
        return scrubbed

    out["observed"] = _rewrite(obs)
    out["cause"] = _rewrite(cause)
    if "reason was not established" not in (
        f"{out.get('observed')}\n{out.get('cause')}"
    ).lower():
        out["observed"] = (
            f"{str(out.get('observed') or '').rstrip()}\n{replacement}"
        ).strip()
    return out


_PSEUDOPORT_RE = re.compile(r"(?i)(?:evpn\s+)?pseudo[- ]?port")
_FLOOD_OVERCLAIM_RE = re.compile(
    r"(?i)(?:i\.e\.?,?\s*)?(?:evpn\s+)?flood\s+state\s+present|"
    r"(?:expected\s+)?(?:remote\s+)?peer.{0,40}(?:installed|present|in)\s+"
    r"(?:the\s+)?(?:flood|replication)\s+list|"
    r"(?:flood|replication)\s+list.{0,40}(?:installed|present|contains)|"
    r"flood\s+state\s+present\s+in\s+this\s+bd"
)
_FLOOD_DISTINCTION_KEPT_RE = re.compile(
    r"(?i)(?:does\s+not|doesn't|do\s+not|don't).{0,40}"
    r"(?:by\s+itself\s+)?(?:prove|mean|establish|imply).{0,60}"
    r"(?:flood|replication)|"
    r"missing\s+check:.{0,40}(?:flood|replication)|"
    r"(?:flood|replication)[- ]list.{0,40}(?:not\s+checked|unverified|"
    r"not\s+verified|was\s+not)"
)
_PSEUDOPORT_IE_FLOOD_RE = re.compile(
    r"(?i),?\s*i\.e\.?,?\s*(?:evpn\s+)?flood\s+state\s+present"
    r"(?:\s+in\s+this\s+bd(?:\s+on\s+both\s+sides)?)?"
)


def _scrub_l2sts_pseudoport_flood_overclaim(
    finding: dict[str, Any],
) -> dict[str, Any]:
    """Pseudo-port up ≠ expected remote peer in the flood list.

    Wording hygiene only — does not demote dataplane status. Keeps the
    distinction unless prose already names the missing peer-specific check
    or states that flood-list membership was verified separately.
    """
    obs = str(finding.get("observed") or "")
    cause = str(finding.get("cause") or "")
    blob = f"{obs}\n{cause}"
    if not _PSEUDOPORT_RE.search(blob):
        return finding
    if _FLOOD_DISTINCTION_KEPT_RE.search(blob):
        return finding
    if not _FLOOD_OVERCLAIM_RE.search(blob):
        return finding

    out = dict(finding)
    clarification = (
        "EVPN pseudo-port up (does not by itself prove the expected remote "
        "peer is installed in the flood list; missing check: peer-specific "
        "replication / flood-list state)"
    )

    def _rewrite(text: str) -> str:
        if not text.strip():
            return text
        scrubbed = _PSEUDOPORT_IE_FLOOD_RE.sub(f" — {clarification}", text)
        scrubbed = re.sub(
            r"(?i)(?:i\.e\.?,?\s*)?(?:evpn\s+)?flood\s+state\s+present"
            r"(?:\s+in\s+this\s+bd(?:\s+on\s+both\s+sides)?)?",
            clarification,
            scrubbed,
        )
        if "does not by itself prove" not in scrubbed.lower():
            # Append once when an overclaim remains without the distinction.
            if _FLOOD_OVERCLAIM_RE.search(scrubbed) or (
                _PSEUDOPORT_RE.search(scrubbed)
                and re.search(r"(?i)flood\s+state", scrubbed)
            ):
                scrubbed = f"{scrubbed.rstrip()} {clarification}."
        return scrubbed

    out["observed"] = _rewrite(obs)
    out["cause"] = _rewrite(cause)
    return out


def _l2sts_up_admits_incomplete_bidirectional_proof(text: str) -> str | None:
    """If LLM prose admits missing two-way EVPN/MAC/RT proof, demote up → unknown.

    Catches Gemma-style ``partially verified`` / one-sided MAC while claiming up.
    Does not parse CLI; relies on the model's own admission in observed/cause.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    lower = raw.lower()

    if "partially verified" in lower or "partially confirm" in lower:
        return (
            "l2sts up refused: conclusion admits only partial forwarding "
            "verification (need bidirectional service-scoped EVPN/RT/MAC "
            "evidence, else unknown)"
        )

    # Empty MAC / no type-2 either direction (nso23 fabric_network-5194 /
    # l2_ctrl_ue_upf-1697 passed on IMET-only while MAX_WASH stayed unknown).
    if _l2sts_admits_no_type2_either_direction(lower):
        return (
            "l2sts up refused: conclusion admits no type-2/BD MAC install "
            "in either direction (IMET or pseudo-port up is supporting "
            "evidence only — conclude unknown)"
        )

    # Numeric RT text gaps are OK when BD-scoped EVPN install is the proof
    # (nso22/nso23 false demote: "not retrieved" + "bidirectional EVPN").
    proof_text = _strip_numeric_rt_text_gaps(lower)

    # Bidirectional MAC/EVPN/RT explicitly not confirmed.
    if re.search(
        r"bidirectional\s+(mac|evpn|rt|route[- ]target|learning|distribution|"
        r"forwarding)",
        proof_text,
    ) and re.search(
        r"(could not|couldn't|cannot|can't|not\s+(be\s+)?(fully\s+)?"
        r"(confirm|verif|establish|obtain|retriev)|unable\s+to\s+(confirm|"
        r"verif|establish)|not\s+confirmed|unconfirmed|incomplete)",
        proof_text,
    ):
        return (
            "l2sts up refused: conclusion admits bidirectional MAC/EVPN/RT "
            "evidence was not established (conclude unknown)"
        )

    # One PE MAC/forwarding table missing while claiming readiness.
    if re.search(r"\bmac\s+table\b", lower) and re.search(
        r"(could not|couldn't|cannot|can't|not\s+retriev|was\s+not\s+retriev|"
        r"unavailable|failed\s+to\s+(get|retriev|collect)|unable\s+to\s+"
        r"(get|retriev|collect))",
        lower,
    ):
        return (
            "l2sts up refused: conclusion admits a PE MAC/forwarding table "
            "was not obtained (one-way evidence is not bidirectional proof)"
        )

    # Explicit one-direction-only while up.
    if re.search(
        r"(only\s+one\s+direction|one[- ]way\s+only|unidirectional\s+"
        r"(mac|evpn|learning)|a\s*→\s*b\s+only|single[- ]direction)",
        lower,
    ) and not re.search(
        r"bidirectional\s+(mac|evpn|rt|learning|distribution).{0,40}"
        r"(confirm|verif|establish|both\s+directions)",
        lower,
    ):
        return (
            "l2sts up refused: conclusion describes only one-direction "
            "MAC/EVPN evidence (need both directions or equivalent RT proof)"
        )

    claims_two_way = bool(
        re.search(
            r"(both directions|bidirectional|"
            r"forwarding evidence exists in both|"
            r"effective rt|rt import/export compatibility|"
            r"demonstrates effective rt)",
            lower,
        )
    )
    counter_as_reverse = bool(
        re.search(
            r"(delivery evidence|crossed the evpn|frames crossed|"
            r"matched by.{0,40}ac (sent|received)|"
            r"ac (received|sent).{0,80}ac (received|sent))",
            lower,
        )
        and re.search(r"\b(pkt|pkts|packet|packets|bytes)\b", lower)
    )
    missing_remote_one_side = bool(
        re.search(
            r"(shows no remote|no remote (evpn )?macs?|zero remote|"
            r"without remote (evpn )?mac|no remote evpn entries)",
            lower,
        )
    )
    aging_hypothesis = bool(
        re.search(
            r"(inactivity aging|aging,? not (with|a)|"
            r"consistent with.{0,60}(low|aging|inactiv|traffic))",
            lower,
        )
    )
    # nso17-style: peer has no local MACs ⇒ claim missing remotes are
    # "not an import failure" (does not test far-end import/install).
    no_local_macs_story = bool(
        re.search(
            r"(no locally learned|zero locally learned|"
            r"no local macs?|zero local macs?|"
            r"contains only the .{0,40}(utah|remote|evpn).{0,20}entries)",
            lower,
        )
    )
    import_ruled_out = bool(
        re.search(
            r"(not (caused )?by (an )?(rt or )?import|"
            r"not by an rt or import|"
            r"not (an )?(rt|import) failure|"
            r"rules? out (an )?(rt|import)|"
            r"import (is )?(fine|ok|working|successful)|"
            r"explained by.{0,100}(locally learned|local macs?).{0,80}"
            r"not by)",
            lower,
        )
    )

    # Opus-style: one-way MAC + counters (or aging story) as "both directions".
    if claims_two_way and counter_as_reverse:
        return (
            "l2sts up refused: AC/packet counters are not bidirectional "
            "EVPN/RT proof (need remote MAC or EVPN install on both PEs, "
            "or equivalent RT evidence)"
        )
    if claims_two_way and missing_remote_one_side and (
        counter_as_reverse or aging_hypothesis
    ):
        return (
            "l2sts up refused: reverse direction not established with "
            "service-scoped EVPN/MAC evidence (counters or unverified "
            "aging/traffic explanations do not suffice)"
        )
    if import_ruled_out or (
        missing_remote_one_side
        and no_local_macs_story
        and (
            claims_two_way
            or re.search(r"not by an rt|not by.{0,20}import", lower)
        )
    ):
        return (
            "l2sts up refused: peer having no locally learned MACs does not "
            "prove far-end import/install would succeed — do not claim "
            "missing remote MACs are not an import failure; keep reverse "
            "service-level install unverified"
        )

    return None


def _l2sts_admits_no_type2_either_direction(lower: str) -> bool:
    """True when prose admits empty MAC / no type-2 install both ways.

    Distinguishes IMET-only "PE-side ready" claims from real bidirectional
    BD MAC/EVPN install. Flood-list gaps alone are not this signal.
    """
    if not lower:
        return False
    # Affirmed bidirectional type-2 / BD EVPN MAC install → not this gap.
    if re.search(
        r"("
        r"installed as (an )?evpn entry in the opposite|"
        r"each pe.s locally learned mac installed|"
        r"type evpn.{0,60}(both|reverse|opposite)|"
        r"bd-scoped bidirectional evpn (mac )?install|"
        r"remote mac.{0,40}(both directions|either direction)|"
        r"(both directions|bidirectional).{0,40}"
        r"(mac|type-2|evpn install|bd install)"
        r".{0,40}(confirm|proven|demonstrat|establish)"
        r")",
        lower,
    ):
        return False
    empty_or_no_type2 = bool(
        re.search(
            r"("
            r"0 mac addresses|"
            r"0 macs?\b|"
            r"zero macs?\b|"
            r"no mac entries|"
            r"mac tables? (for this bd )?(are )?empty|"
            r"bd mac-address tables? empty|"
            r"no type-2|"
            r"no type-2 mac distribution|"
            r"no mac/evpn install|"
            r"no mac entries for either ac|"
            r"no type-2 mac distribution was (observed|demonstrated)"
            r")",
            lower,
        )
    )
    both_or_either = bool(
        re.search(
            r"("
            r"both pes?|"
            r"both endpoints|"
            r"either direction|"
            r"neither direction|"
            r"in either direction|"
            r"both directions|"
            r"on both pes?"
            r")",
            lower,
        )
    )
    return empty_or_no_type2 and both_or_either


def _strip_numeric_rt_text_gaps(lower: str) -> str:
    """Remove numeric/effective RT quote gaps so they do not trip bi-proof gate.

    BD-scoped EVPN MAC install can establish effective RT compatibility without
    quoting numeric RT strings. Phrases like ``numeric RT text not retrieved``
    must not combine with ``bidirectional EVPN install`` into a false demote.
    """
    text = lower
    patterns = (
        # "numeric RT text not needed/not retrieved"
        r"(numeric\s*/\s*effective|numeric|effective)\s+"
        r"rt\s*(text|values?)?\s*"
        r"(not\s+needed\s*/\s*)?not\s+(quoted|retriev\w*|obtain\w*)",
        r"(numeric\s+)?rt\s+text\s+not\s+needed\s*/\s*not\s+retriev\w*",
        r"numeric/effective\s+rt\s+values?\s+not\s+quoted"
        r"(?:\s*\([^)]*bidirectional[^)]*\))?",
        r"\(numeric\s+rt\s+text\s+not\s+needed\s*/\s*not\s+retriev\w*\)",
        r"numeric\s+rts?\s+being\s+(?:not\s+)?retriev\w*",
        r"without\s+numeric\s+rts?\s+being\s+retriev\w*",
        # nso23: "numeric EVI/RT text was not obtainable"
        r"numeric\s+(?:evi\s*/\s*)?rt\s*(?:text|values?)?\s*"
        r"(?:was\s+|were\s+)?not\s+obtain\w*",
        # nso23: "numeric RT values could not be read"
        r"numeric\s+(?:evi\s*/\s*)?rt\s*(?:text|values?)?\s*"
        r"could\s+not\s+be\s+(?:read|retriev\w*|obtain\w*|quot\w*)",
        r"even\s+though\s+numeric\s+rt\s+values?\s+could\s+not\s+be\s+read",
        r"numeric\s+(?:evi\s*/\s*)?rt\s*(?:text|values?)?\s*"
        r"(?:could\s+not|cannot|can't)\s+(?:be\s+)?(?:read|retriev\w*|obtain\w*)",
    )
    for pat in patterns:
        text = re.sub(pat, " ", text)
    return text


# LLM often invents bare "show evpn …" or "show l2vpn evpn …"; lab XR stubs.
_BARE_EVPN_SHOW_RE = re.compile(r"(?i)^(?:show\s+)?evpn(\s|$)")
_L2VPN_EVPN_SHOW_RE = re.compile(r"(?i)^(?:show\s+)?l2vpn\s+evpn(\s|$)")
# Invalid on XR: "show l2vpn bridge-domain … mac|mac-address|mac learned"
_BD_MAC_SUFFIX_RE = re.compile(
    r"(?i)^(?:show\s+)?l2vpn\s+bridge-domain\b.*\bmac(?:-address)?(?:\s+learned)?\s*$"
)
# Incomplete on these PEs without location: "l2vpn forwarding … mac-address"
_FWD_MAC_NO_LOC_RE = re.compile(
    r"(?i)^(?:show\s+)?l2vpn\s+forwarding\b.*\bmac-address\b(?!.*\blocation\b)"
)
# Invalid on these PEs: "bgp l2vpn evpn evi <n>" (summary/detail/all are OK)
_BGP_EVPN_EVI_RE = re.compile(
    r"(?i)^(?:show\s+)?bgp\s+l2vpn\s+evpn\s+evi\b"
)
# RESTCONF paths must be module-qualified (RFC 8040), e.g. tailf-ncs:devices/...
_MODULE_QUALIFIED_PATH_RE = re.compile(r"^[A-Za-z_][\w.-]*:")


def _partial_has_l2sts_rt_ladder(partial_results: list[dict[str, Any]]) -> bool:
    """True when get_device_config + bgp l2vpn evpn already returned this dig."""
    has_config = False
    has_bgp = False
    for pr in partial_results or []:
        if not isinstance(pr, dict):
            continue
        tool = str(pr.get("tool") or "")
        outcome = str(pr.get("outcome") or "")
        params = pr.get("params") if isinstance(pr.get("params"), dict) else {}
        if tool == "get_device_config" and outcome == "returned":
            has_config = True
        cmd = str(
            params.get("input_command") or params.get("command") or ""
        ).lower()
        if (
            tool == "exec_show"
            and outcome == "returned"
            and "bgp l2vpn evpn" in cmd
        ):
            has_bgp = True
    return has_config and has_bgp


def disallowed_dataplane_show_command(command: str) -> str | None:
    """Reject known-bad / incomplete show forms that waste dig budget on lab XR."""
    raw = (command or "").strip()
    if not raw:
        return None
    cmd = re.sub(r"(?i)^show\s+", "", raw).strip()
    # ``run evpn`` is almost always rejected on this exec path; treating it as
    # a soft-block saves multi-minute dig rounds (nso21: late run evpn → 96s
    # LLM turn). Prefer get_device_config + bgp l2vpn evpn rd.
    if re.match(r"(?i)^run\s+evpn\b", cmd):
        return (
            "Rejected: 'run evpn' is usually rejected by the exec path here "
            "and wastes dig rounds. Prefer get_device_config for EVI/RT text, "
            "'bgp l2vpn evpn summary', and 'bgp l2vpn evpn rd <rd>'. If those "
            "already returned, that alone is not sufficient evidence — if "
            "numeric/effective RT or another service-specific check is still "
            "missing, conclude with dataplane_status=unknown and name that "
            "missing check. Do not invent show evpn / evpn evi variants, "
            "and do not keep fishing with unrelated AC interface shows for "
            "RT proof."
        )
    if re.match(r"(?i)^run\s*\|\s*include\b", cmd):
        return (
            "Rejected: 'run | include …' is often rejected by the exec path. "
            "Prefer get_device_config for EVI/RT text."
        )
    # Allow BGP EVPN AF summary/detail/all — but not "… evi <n>" (invalid here).
    if re.match(r"(?i)^bgp\s+l2vpn\s+evpn\b", cmd):
        if _BGP_EVPN_EVI_RE.match(cmd):
            return (
                "Rejected: 'show bgp l2vpn evpn evi <n>' is invalid on these "
                "IOS-XR PEs. Use 'bgp l2vpn evpn summary' (or detail/all), "
                "'bgp l2vpn evpn rd <rd>', and get_device_config for EVI/RT. "
                "Do not invent 'evpn evi …' shows."
            )
        return None
    if _BARE_EVPN_SHOW_RE.match(cmd) or _L2VPN_EVPN_SHOW_RE.match(cmd):
        return (
            "Rejected: 'show evpn …' / 'show l2vpn evpn …' / 'evpn evi …' are "
            "invalid or empty on these IOS-XR PEs and waste budget. Do NOT "
            "retry synonym variants. Prefer get_device_config for EVI/RT, "
            "'bgp l2vpn evpn summary', and 'bgp l2vpn evpn rd <rd>' for "
            "peer EVPN routes (use the peer PE's RD:EVI from config/BD "
            "detail). Also OK: 'run l2vpn', BD brief/detail, AC "
            "'interfaces …'. Config alone is not enough to declare up — "
            "use operational evidence, or conclude unknown if readiness "
            "is still unproven."
        )
    if _BD_MAC_SUFFIX_RE.match(cmd):
        return (
            "Rejected: 'show l2vpn bridge-domain … mac|mac-address|mac learned' "
            "is invalid IOS-XR syntax and wastes budget. MAC learning is "
            "optional; empty MACs are not a fault. If needed use "
            "'l2vpn forwarding bridge-domain mac-address location 0/RP0/CPU0'. "
            "Prefer BD brief/detail and get_device_config for health."
        )
    if _FWD_MAC_NO_LOC_RE.search(cmd):
        return (
            "Rejected: 'show l2vpn forwarding bridge-domain mac-address' is "
            "incomplete without 'location <node>' on these PEs "
            "(returns 'Incomplete command'). Use "
            "'l2vpn forwarding bridge-domain mac-address location 0/RP0/CPU0' "
            "or skip MAC checks — empty MAC tables are not a dataplane fault "
            "when customer traffic is unknown."
        )
    return None


def disallowed_explore_nso_path(path: str) -> str | None:
    """Reject explore paths that are not module-qualified RESTCONF paths."""
    raw = (path or "").strip()
    if not raw:
        return (
            "Rejected: explore_nso_path needs a non-empty RESTCONF path "
            "(e.g. 'tailf-ncs:devices/device=renc-data-sw'). For service "
            "instances prefer get_services / check_service_sync / "
            "compare_service_config."
        )
    # Drop a single leading slash; still require module: afterward.
    stripped = raw[1:] if raw.startswith("/") else raw
    if stripped.lower().startswith("services/") or stripped.lower() == "services":
        return (
            "Rejected: bare '/services/…' is not a valid RESTCONF path "
            "(missing YANG module name). Prefer get_services(service_type) "
            "or check_service_sync(service_type, service_name). If you must "
            "explore, use a module-qualified path "
            "(e.g. 'tailf-ncs:devices/device=<name>')."
        )
    if not _MODULE_QUALIFIED_PATH_RE.match(stripped):
        return (
            "Rejected: explore_nso_path path must be module-qualified "
            "(e.g. 'tailf-ncs:devices/device=<name>'), not "
            f"{raw!r}. Prefer get_services / get_device_config / exec_show "
            "for service dataplane work."
        )
    # Root NED modules are not top-level RESTCONF data; device config is under
    # tailf-ncs:devices/device=<name>/config/…
    if re.match(r"(?i)^tailf-ned-cisco-ios-xr:", stripped):
        return (
            "Rejected: root 'tailf-ned-cisco-ios-xr:…' is not a valid NSO "
            "data path (RESTCONF 404). Prefer get_device_config(device_name) "
            "or exec_show 'run l2vpn' / 'l2vpn bridge-domain brief'. "
            "Do not explore NED roots."
        )
    # XR EVPN is not under l2vpn/evpn in this NED — repeated 404 fishing.
    if re.search(r"(?i)l2vpn/evpn(?:/|$)", stripped):
        return (
            "Rejected: '…/l2vpn/evpn' is not a valid keypath on these XR "
            "NEDs (RESTCONF uri keypath not found). Prefer "
            "get_device_config(device_name) and/or exec_show "
            "'run l2vpn' / 'l2vpn bridge-domain brief' / "
            "'bgp l2vpn evpn rd <rd>'. Do not retry explore_nso_path for "
            "EVPN/L2VPN config, and do not fall back to 'run evpn'."
        )
    return None


def coerce_dataplane_mcp_call(
    tool_name: str,
    params: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], str | None]:
    """Normalize mcp_call args; recover omitted tool_name when params are clear.

    Some models emit ``mcp_call`` with ``input_command`` but leave ``tool_name``
    blank (see l3rt digs). When params are unambiguously ``exec_show``-shaped,
    fill ``tool_name=exec_show``. Returns ``(tool_name, params, note)`` where
    note is a short log hint if coercion happened, else None.
    """
    name = str(tool_name or "").strip()
    args = dict(params or {})
    # Mistaken nesting: tool_name inside params.
    nested = args.pop("tool_name", None)
    if not name and isinstance(nested, str) and nested.strip():
        name = nested.strip()
        return name, args, f"lifted tool_name={name!r} from params"

    if name:
        return name, args, None

    has_cmd = bool(
        str(args.get("input_command") or args.get("command") or "").strip()
    )
    has_device = bool(
        str(args.get("device_name") or args.get("device") or "").strip()
    )
    if has_cmd and has_device:
        return "exec_show", args, "inferred tool_name=exec_show from CLI params"

    return "", args, None


def empty_mcp_tool_name_error(params: dict[str, Any] | None) -> str:
    """Clear ERROR when tool_name is missing and cannot be inferred."""
    allowed = ", ".join(sorted(DATAPLANE_ALLOWLIST))
    hint = (
        "For device CLI shows set tool_name=exec_show with "
        "params.device_name + params.input_command."
    )
    keys = sorted((params or {}).keys())
    got = f" params keys={keys}" if keys else ""
    return (
        f"ERROR: mcp_call requires non-empty tool_name "
        f"(allowed: {allowed}). {hint}{got}"
    )


def clip_dataplane_tool_content(
    tool_name: str,
    text: str,
    *,
    service_name: str = "",
    limit: int = _DATAPLANE_RESULT_CLIP,
) -> str:
    """Format MCP tool output for dig LLM: unwrap envelope, keep newlines, clip body."""
    from nso_facts.mcp_client import unwrap_mcp_result_text

    tname = (tool_name or "").strip() or "tool"
    raw_in = text if isinstance(text, str) else json.dumps(text, default=str)
    try:
        parsed: Any = json.loads(raw_in)
    except (json.JSONDecodeError, TypeError):
        parsed = raw_in
    body = unwrap_mcp_result_text(parsed)
    original_len = len(body)

    config_like = tname in {
        "get_device_config",
        "explore_nso_path",
        "compare_device_config",
        "compare_service_config",
    } or (
        tname == "exec_show"
        and re.search(
            r"(?i)(\brun\b|\bl2vpn\b|\bevpn\b|\broute-target\b)",
            body[:1200],
        )
    )

    selected = body
    selection_note = ""
    if config_like and original_len > limit:
        svc_bits = [
            p for p in re.split(r"[-_]", str(service_name or "")) if len(p) >= 4
        ][:4]
        lines = body.splitlines()
        kept: list[str] = []
        for line in lines:
            if _CONFIG_KEEP_RE.search(line):
                kept.append(line)
                continue
            if svc_bits and any(b.lower() in line.lower() for b in svc_bits):
                kept.append(line)
        if kept:
            selected = "\n".join(kept)
            selection_note = (
                f"relevant lines {len(kept)}/{len(lines)}; "
            )

    if len(selected) <= limit:
        if selection_note:
            return (
                f"[filtered {tname}: {selection_note}"
                f"body {original_len} chars]\n"
                + selected
            )
        return selected

    keep = max(0, limit - 96)
    truncated = selected[:keep].rstrip()
    return (
        f"[truncated {tname}: {selection_note}"
        f"showing {keep} of {original_len} chars]\n"
        + truncated
        + f"\n\n[truncated: output cut at {keep} of {original_len} chars]"
    )


def _record_dataplane_finding(
    case: CaseFile,
    record: dict[str, Any],
    finding: dict[str, Any],
    *,
    source: str,
    evidence_ids: list[str] | None = None,
) -> None:
    """Record a *complete* dataplane conclusion (investigation finished + explained)."""
    from diagnostic_mas.case import add_diagnosis, set_issue_status

    apply_dataplane_status(record, finding["dataplane_status"])
    payload = {
        "service_type": record.get("service_type"),
        "name": record.get("name"),
        "source": source,
        **{k: v for k, v in finding.items() if k != "complete"},
        "complete": True,
    }
    add_evidence(
        case,
        {
            "kind": "dataplane_finding",
            "role": "dataplane",
            "layer": "services",
            "payload": payload,
        },
    )
    subject: dict[str, Any] = {}
    if record.get("service_type") is not None:
        subject["service_type"] = record.get("service_type")
    if record.get("name") is not None:
        subject["name"] = record.get("name")
    add_diagnosis(
        case,
        kind="dataplane",
        source=source,
        status=str(finding["dataplane_status"]),
        subject=subject or None,
        observed=str(finding.get("observed") or ""),
        cause=str(finding.get("cause") or ""),
        fix_suggestion=finding.get("fix_suggestion")
        if isinstance(finding.get("fix_suggestion"), str)
        else None,
        confidence=str(finding.get("confidence") or "medium"),
        evidence_ids=list(evidence_ids or []),
        extra={"complete": True, "verification_gap": finding.get("verification_gap")},
    )
    record["status"] = combine_service_status(
        str(record.get("system_status") or "unknown"),
        str(record.get("dataplane_status") or "not_checked"),
    )
    name = record.get("name")
    if isinstance(name, str) and name:
        for issue in case.issues:
            if (
                issue.get("layer") == "services"
                and issue.get("edge_id") == name
                and issue.get("status") == "open"
            ):
                set_issue_status(case, issue["id"], "explained")


def _record_incomplete_dataplane_verify(
    case: CaseFile,
    record: dict[str, Any],
    finding: dict[str, Any],
    *,
    evidence_ids: list[str] | None = None,
    source: str | None = None,
) -> None:
    """Unresolved dataplane outcome: issue *not* explained.

    Covers timeout/budget, gate-rejected ``up``, and incomplete LLM digs.
    Records ``dataplane_status=unknown`` with ``complete=False`` for the
    DataplaneDig inconclusive tally, but does **not** demote overall
    ``status`` / SystemUp — only a finished dig that finds down, degraded,
    or unknown does that. Leaves matching Issues ``open`` so drill follow-up
    can still run when drill budget remains (do not use ``budget_exhausted``
    — that status is excluded from drill selection).
    """
    from diagnostic_mas.case import add_diagnosis

    # Dig inconclusive for reporting; leave overall status on system/sync layer.
    record["dataplane_status"] = "unknown"
    sys = str(record.get("system_status") or "unknown").lower()
    if sys not in {"up", "down", "degraded", "unknown"}:
        sys = "unknown"
    record["status"] = sys
    if source:
        src = source
    elif str(finding.get("cause") or "").startswith("[gate]"):
        src = "gate"
    else:
        src = "fallback"
    payload = {
        "service_type": record.get("service_type"),
        "name": record.get("name"),
        "source": src,
        **{
            k: v
            for k, v in finding.items()
            if k not in {"complete", "dataplane_status", "source"}
        },
        "dataplane_status": "unknown",
        "complete": False,
    }
    add_evidence(
        case,
        {
            "kind": "dataplane_incomplete",
            "role": "dataplane",
            "layer": "services",
            "payload": payload,
        },
    )
    subject: dict[str, Any] = {}
    if record.get("service_type") is not None:
        subject["service_type"] = record.get("service_type")
    if record.get("name") is not None:
        subject["name"] = record.get("name")
    add_diagnosis(
        case,
        kind="dataplane",
        source=src,
        status="unknown",
        subject=subject or None,
        observed=str(finding.get("observed") or ""),
        cause=str(finding.get("cause") or ""),
        fix_suggestion=None,
        confidence="low",
        evidence_ids=list(evidence_ids or []),
        extra={"complete": False, "verification_gap": finding.get("verification_gap")},
    )
    # Intentionally leave open service Issues open — unresolved, not explained.
    # Overall status already restored to system_status above (SystemUp preserved).


def parse_dataplane_conclusion(data: Any) -> dict[str, Any] | None:
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
    dp = str(data.get("dataplane_status") or "").lower()
    if dp not in {"up", "down", "degraded", "unknown"}:
        return None
    observed = data.get("observed")
    cause = data.get("cause")
    if not (isinstance(observed, str) and observed.strip()):
        return None
    if not (isinstance(cause, str) and cause.strip()):
        return None
    fix = data.get("fix_suggestion")
    conf = str(data.get("confidence") or "medium").lower()
    if conf not in {"high", "medium", "low"}:
        conf = "medium"
    return {
        "dataplane_status": dp,
        "observed": observed.strip(),
        "cause": cause.strip(),
        "fix_suggestion": fix.strip()
        if isinstance(fix, str) and fix.strip()
        else None,
        "confidence": conf,
        **({"verification_gap": normalize_gap(data.get("verification_gap"))}
           if normalize_gap(data.get("verification_gap")) else {}),
    }


def _refresh_spine_counts(ev: dict[str, Any]) -> None:
    extra = _spine_extra(ev)
    if not extra:
        return
    services = extra.get("services")
    if not isinstance(services, dict):
        return
    counts = counts_from_services(services)
    extra["counts"] = counts
    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else None
    if isinstance(payload, dict):
        payload["operational_summary"] = counts


def dataplane_diagnosed_names(case: CaseFile) -> set[str]:
    """Service names with a *complete* dataplane conclusion (issue explained)."""
    names: set[str] = set()
    for dx in case.diagnoses:
        if dx.get("kind") != "dataplane":
            continue
        if dx.get("complete") is False:
            continue
        subject = dx.get("subject") if isinstance(dx.get("subject"), dict) else {}
        for key in ("name", "service_name"):
            n = subject.get(key) if subject else None
            if n is None:
                n = dx.get(key)
            if isinstance(n, str) and n.strip():
                names.add(n.strip())
    if names:
        return names
    # Legacy: diagnoses empty — fall back to complete dataplane_finding Evidence
    for ev in case.evidence:
        if ev.get("kind") != "dataplane_finding":
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if payload.get("complete") is False:
            continue
        for key in ("name", "service_name"):
            n = payload.get(key)
            if isinstance(n, str) and n.strip():
                names.add(n.strip())
    return names


def _open_issues_for_updated_services(case: CaseFile, services: dict[str, Any]) -> None:
    from diagnostic_mas.case import open_issue, set_issue_status

    existing = {
        (i.get("code"), i.get("edge_id"))
        for i in case.issues
        if i.get("layer") == "services"
    }
    diagnosed = dataplane_diagnosed_names(case)
    for issue in issues_from_service_health(services):
        key = (issue.get("code"), issue.get("edge_id"))
        if key in existing:
            # Spine may have opened this earlier; dataplane already concluded.
            edge = issue.get("edge_id")
            if isinstance(edge, str) and edge in diagnosed:
                for i in case.issues:
                    if (
                        i.get("layer") == "services"
                        and i.get("edge_id") == edge
                        and i.get("status") == "open"
                    ):
                        set_issue_status(case, i["id"], "explained")
            continue
        kwargs: dict[str, Any] = {
            "code": str(issue.get("code") or "service"),
            "message": str(issue.get("message") or ""),
            "evidence_ids": [],
            "layer": "services",
            "edge_id": issue.get("edge_id"),
            "devices": issue.get("devices"),
            "severity": str(issue.get("severity") or "medium"),
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
        iid = open_issue(case, **kwargs)
        edge = issue.get("edge_id")
        if isinstance(edge, str) and edge in diagnosed:
            set_issue_status(case, iid, "explained")


async def llm_dataplane_verify_one(
    client: Any, settings: Settings, case: CaseFile, *,
    record: dict[str, Any], device_names: set[str], session: DrillSession,
    openai_client: Any | None = None,
) -> bool:
    """Measure every exit, including provider exceptions, without live side effects."""
    investigation = Investigation()
    diag_start, ev_start = len(case.diagnoses), len(case.evidence)
    before = session.tools_used
    try:
        return await _llm_dataplane_verify_one(
            client, settings, case, record=record, device_names=device_names,
            session=session, openai_client=openai_client, investigation=investigation,
        )
    finally:
        metrics = investigation.finish(session.tools_used-before, session.max_tools-before)
        for dx in case.diagnoses[diag_start:]:
            if dx.get("kind") != "dataplane":
                continue
            dx["investigation"] = metrics
            if dx.get("status") == "unknown":
                if metrics["stop_reason"] != "concluded_insufficient_evidence" or not dx.get("verification_gap"):
                    dx["verification_gap"] = fallback_gap(metrics)
        for ev in case.evidence[ev_start:]:
            if ev.get("kind") not in {"dataplane_finding", "dataplane_incomplete"}:
                continue
            payload = ev["payload"]
            payload["investigation"] = metrics
            if payload.get("dataplane_status") == "unknown":
                if metrics["stop_reason"] != "concluded_insufficient_evidence" or not payload.get("verification_gap"):
                    payload["verification_gap"] = fallback_gap(metrics)


async def _llm_dataplane_verify_one(
    client: Any,
    settings: Settings,
    case: CaseFile,
    *,
    record: dict[str, Any],
    device_names: set[str],
    session: DrillSession,
    openai_client: Any | None = None,
    investigation: Investigation,
) -> bool:
    """Returns True if a dataplane_status was recorded (LLM or fallback)."""
    from agent.summarize import fabric_openai_client
    from diagnostic_mas.case import evidence_ids_after

    before = session.tools_used
    remaining = session.max_tools - session.tools_used
    if remaining <= 0:
        return False

    name = record.get("name") or "?"
    _log(f"start service={name} tools_budget={remaining}")
    ev_watermark = case._evidence_seq

    brief = {
        "service_type": record.get("service_type"),
        "name": record.get("name"),
        "devices": record.get("devices"),
        "system_status": record.get("system_status"),
        "dataplane_status": record.get("dataplane_status"),
        "in_sync": record.get("in_sync"),
        "device_sync": record.get("device_sync"),
    }
    if isinstance(record.get("live_l2"), dict):
        brief["live_l2"] = record["live_l2"]
    try:
        from nso_facts.mcp_client import quarantined_devices

        q = quarantined_devices() or {}
        if q:
            brief["unavailable_devices"] = sorted(q.keys())
            brief["available_devices"] = sorted(
                d for d in (record.get("devices") or []) if d and d not in q
            )
    except Exception:  # noqa: BLE001
        pass

    max_rounds_hint = min(remaining + 2, _DATAPLANE_MAX_ROUNDS_CAP)

    user_bits = [
        f"Check the dataplane status of this "
        f"{record.get('service_type') or 'NSO'} service.",
        f"At most {remaining} mcp_call tools; conclude by round {max_rounds_hint}.",
        "Do not call MCP on unavailable_devices; prefer available_devices.",
        "check_service_sync needs service_type + service_name; "
        "never ping via exec_show.",
        "",
        json.dumps(brief, indent=2, default=str),
    ]

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": dataplane_system_prompt(record.get("service_type")),
        },
        {"role": "user", "content": "\n".join(user_bits)},
    ]
    try:
        catalog = await tool_catalog(client, DATAPLANE_ALLOWLIST)
        # Fabric/Qwen: only one system message, and it must be first.
        messages[0]["content"] = (
            str(messages[0]["content"]).rstrip() + "\n\n" + catalog
        )
        _log(f"tool schemas supplied ({len(catalog)} chars)")
    except Exception as exc:  # discovery failure must be visible
        _log(f"tool schema discovery unavailable: {type(exc).__name__}")
    progress = BatchProgress()
    conclude_only = False
    force_conclude_choice = False
    empty_turn_nudge_used = False
    stop_reason = "investigation ended without a conclusion"
    partial_results: list[dict[str, Any]] = []
    oai = openai_client or fabric_openai_client(
        settings, timeout=FABRIC_CHAT_TIMEOUT_SEC
    )
    concluded = False
    max_rounds = max_rounds_hint
    investigation.round_limit = max_rounds
    service_name = str(record.get("name") or "")
    # After the last mcp_call depletes the budget, allow one more LLM turn so
    # conclude_dataplane can evaluate that result (do not break on remaining==0).
    await_conclusion = False

    for round_i in range(max_rounds):
        remaining = session.max_tools - session.tools_used
        if remaining <= 0 and not await_conclusion:
            break
        if concluded:
            break
        _log(
            f"LLM round {round_i + 1}/{max_rounds} "
            f"(tools_left={remaining}"
            f"{', conclude_only' if conclude_only or remaining <= 0 else ''})"
        )
        if remaining <= 0:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Tool budget is exhausted. Call conclude_dataplane now "
                        "using the evidence already returned (unknown if unsure)."
                    ),
                }
            )
        await_conclusion = False
        investigation.rounds = round_i + 1
        llm_started = time.monotonic()
        tools_payload = (
            [CONCLUDE_DATAPLANE_TOOL] if conclude_only else DATAPLANE_TOOLS
        )
        tool_choice: Any = "auto"
        if force_conclude_choice:
            tool_choice = {
                "type": "function",
                "function": {"name": "conclude_dataplane"},
            }
            force_conclude_choice = False
        msg_chars = _messages_chars(messages)
        tools_chars = len(json.dumps(tools_payload, default=str))
        _log(
            f"LLM request round={round_i + 1} "
            f"messages={msg_chars} chars tools={tools_chars} chars "
            f"total≈{msg_chars + tools_chars} chars"
        )
        try:
            response = investigation.llm(oai.chat.completions.create,
                model=settings.fabric_model,
                messages=messages,
                tools=tools_payload,
                tool_choice=tool_choice,
                temperature=0.1,
            )
        except Exception as exc:  # noqa: BLE001
            from agent.llm_budget import (
                ProviderBudgetExceeded,
                is_provider_budget_exceeded,
                mark_case_llm_halt,
                provider_budget_message,
            )

            elapsed = time.monotonic() - llm_started
            if is_provider_budget_exceeded(exc):
                msg = provider_budget_message(exc)
                mark_case_llm_halt(
                    case, f"provider budget_exceeded: {msg}"
                )
                stop_reason = f"provider LLM budget exceeded: {msg}"
                _log(
                    f"LLM budget exceeded after {elapsed:.2f}s — "
                    f"stopping further LLM requests: {exc}"
                )
                dump_path = _dump_failed_dataplane_llm_request(
                    service_name=service_name,
                    round_i=round_i + 1,
                    model=str(settings.fabric_model),
                    messages=messages,
                    tools=tools_payload,
                    error=exc,
                    elapsed_s=elapsed,
                )
                if dump_path is not None:
                    _log(f"dumped failed LLM request to {dump_path}")
                if not concluded:
                    finding = _incomplete_verify_finding(
                        record, stop_reason=stop_reason
                    )
                    finding["partial_tool_results"] = partial_results
                    _record_incomplete_dataplane_verify(
                        case,
                        record,
                        finding,
                        evidence_ids=evidence_ids_after(case, ev_watermark),
                        source="llm",
                    )
                    _log(f"incomplete: unknown for {name} (provider budget)")
                raise ProviderBudgetExceeded(
                    case.llm_halt_reason or msg
                ) from exc
            # Some Fabric models reject forced function tool_choice; retry once
            # with auto when we were only forcing conclude_dataplane.
            if tool_choice != "auto":
                _log(
                    f"forced tool_choice rejected ({type(exc).__name__}); "
                    "retrying with tool_choice=auto"
                )
                try:
                    response = investigation.llm(oai.chat.completions.create,
                        model=settings.fabric_model,
                        messages=messages,
                        tools=tools_payload,
                        tool_choice="auto",
                        temperature=0.1,
                    )
                except Exception as exc2:  # noqa: BLE001
                    elapsed = time.monotonic() - llm_started
                    if is_provider_budget_exceeded(exc2):
                        msg = provider_budget_message(exc2)
                        mark_case_llm_halt(
                            case, f"provider budget_exceeded: {msg}"
                        )
                        stop_reason = f"provider LLM budget exceeded: {msg}"
                        _log(
                            f"LLM budget exceeded after {elapsed:.2f}s — "
                            f"stopping further LLM requests: {exc2}"
                        )
                        if not concluded:
                            finding = _incomplete_verify_finding(
                                record, stop_reason=stop_reason
                            )
                            finding["partial_tool_results"] = partial_results
                            _record_incomplete_dataplane_verify(
                                case,
                                record,
                                finding,
                                evidence_ids=evidence_ids_after(
                                    case, ev_watermark
                                ),
                                source="llm",
                            )
                        raise ProviderBudgetExceeded(
                            case.llm_halt_reason or msg
                        ) from exc2
                    stop_reason = f"LLM request failed: {type(exc2).__name__}"
                    _log(f"LLM error after {elapsed:.2f}s: {exc2}")
                    dump_path = _dump_failed_dataplane_llm_request(
                        service_name=service_name,
                        round_i=round_i + 1,
                        model=str(settings.fabric_model),
                        messages=messages,
                        tools=tools_payload,
                        error=exc2,
                        elapsed_s=elapsed,
                    )
                    if dump_path is not None:
                        _log(f"dumped failed LLM request to {dump_path}")
                    break
            else:
                stop_reason = f"LLM request failed: {type(exc).__name__}"
                _log(f"LLM error after {elapsed:.2f}s: {exc}")
                dump_path = _dump_failed_dataplane_llm_request(
                    service_name=service_name,
                    round_i=round_i + 1,
                    model=str(settings.fabric_model),
                    messages=messages,
                    tools=tools_payload,
                    error=exc,
                    elapsed_s=elapsed,
                )
                if dump_path is not None:
                    _log(f"dumped failed LLM request to {dump_path}")
                break
        _log(f"LLM round completed in {time.monotonic() - llm_started:.2f}s")
        batch_responses: list[tuple[str, str]] = []
        batch_message_start = len(messages)
        blocked_unavailable_rt_path = False
        choice0 = response.choices[0]
        message = choice0.message
        finish_reason = getattr(choice0, "finish_reason", None)
        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            finding = parse_dataplane_conclusion(message.content or "")
            if finding:
                ev_ids = evidence_ids_after(case, ev_watermark)
                session_ev = [
                    e for e in case.evidence if e.get("id") in set(ev_ids)
                ]
                finding = accept_dataplane_conclusion(
                    record, finding, session_evidence=session_ev
                )
                investigation.concluded(finding)
                _commit_dataplane_conclusion(
                    case,
                    record,
                    finding,
                    source="llm",
                    evidence_ids=ev_ids,
                )
                _log(
                    f"conclude: {record.get('dataplane_status')} "
                    f"{finding.get('cause', '')[:80]}"
                )
                concluded = True
                break
            turn_diag = _empty_tool_turn_log_line(
                message, finish_reason=finish_reason
            )
            _log(f"LLM returned no tools / no conclusion — {turn_diag}")
            if not empty_turn_nudge_used:
                empty_turn_nudge_used = True
                messages.append(_message_to_dict(message))
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your last reply had neither mcp_call nor "
                            "conclude_dataplane. Call conclude_dataplane now "
                            "using the evidence already returned (unknown if "
                            "unsure). Do not answer in prose only."
                        ),
                    }
                )
                conclude_only = True
                force_conclude_choice = True
                _log("nudging once: force conclude_dataplane")
                continue
            stop_reason = "LLM returned no tools / no conclusion"
            _log("LLM returned no tools / no conclusion — stop (after nudge)")
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

            if fn_name == "conclude_dataplane":
                finding = parse_dataplane_conclusion(args)
                if finding:
                    ev_ids = evidence_ids_after(case, ev_watermark)
                    session_ev = [
                        e for e in case.evidence if e.get("id") in set(ev_ids)
                    ]
                    finding = accept_dataplane_conclusion(
                        record, finding, session_evidence=session_ev
                    )
                    investigation.concluded(finding)
                    _commit_dataplane_conclusion(
                        case,
                        record,
                        finding,
                        source="llm",
                        evidence_ids=ev_ids,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps({"ok": True, "recorded": True}),
                        }
                    )
                    _log(
                        f"conclude: {record.get('dataplane_status')} "
                        f"{finding.get('cause', '')[:100]}"
                    )
                    concluded = True
                else:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": (
                                "ERROR: conclude_dataplane needs "
                                "dataplane_status + observed + cause"
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
                            "use mcp_call or conclude_dataplane"
                        ),
                    }
                )
                continue

            if conclude_only:
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": "ERROR: no-progress stop; conclude only"})
                continue
            remaining = session.max_tools - session.tools_used
            if remaining <= 0:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": "ERROR: tool budget exhausted — call conclude_dataplane now",
                    }
                )
                await_conclusion = True
                continue

            tool_name = str(args.get("tool_name") or "")
            params = (
                args.get("params") if isinstance(args.get("params"), dict) else {}
            )
            reason = args.get("reason")
            tool_name, params, coerce_note = coerce_dataplane_mcp_call(
                tool_name, params
            )
            if coerce_note:
                _log(f"mcp_call coerce: {coerce_note}")
            if not tool_name:
                _log(
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
            _log(f"mcp_call {tool_name} {json.dumps(params, default=str)[:120]}")
            # Soft-block bare show evpn … before debiting MCP budget.
            if tool_name.strip() == "exec_show":
                show_cmd = str(
                    params.get("input_command")
                    or params.get("command")
                    or ""
                )
                blocked = disallowed_dataplane_show_command(show_cmd)
                if blocked:
                    _log(f"blocked exec_show {show_cmd!r}")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": f"ERROR: {blocked}",
                        }
                    )
                    show_norm = re.sub(
                        r"(?i)^show\s+", "", (show_cmd or "").strip()
                    )
                    if re.match(r"(?i)^run\s+evpn\b", show_norm):
                        blocked_unavailable_rt_path = True
                    continue
            if tool_name.strip() == "explore_nso_path":
                explore_path = str(params.get("path") or "")
                blocked_path = disallowed_explore_nso_path(explore_path)
                if blocked_path:
                    _log(f"blocked explore_nso_path {explore_path!r}")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": f"ERROR: {blocked_path}",
                        }
                    )
                    continue
            mcp_started = time.monotonic()
            result_text = await investigation.mcp(execute_one_drill_call,
                client,
                case,
                tool_name=tool_name,
                params=params,
                reason=str(reason) if reason else None,
                device_names=device_names,
                session=session,
                allowlist=DATAPLANE_ALLOWLIST,
                account="dataplane",
            )
            elapsed = time.monotonic() - mcp_started
            failed = result_error(result_text)
            investigation.query_errors += int(bool(failed))
            _log(f"MCP {tool_name} device={params.get('device_name', params.get('device', '-'))} "
                 f"elapsed={elapsed:.2f}s outcome={'error' if failed else 'returned'}")
            if failed:
                result_text = "ERROR: tool execution/CLI failed; not service-health evidence.\n" + result_text
            batch_responses.append((tool_name, result_text))
            partial_results.append({"tool": tool_name, "params": params,
                                    "outcome": "error" if failed else "returned",
                                    "elapsed_seconds": round(elapsed, 3),
                                    "output_excerpt": result_text[:1500]})
            clipped = clip_dataplane_tool_content(
                tool_name, result_text, service_name=service_name
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": clipped,
                }
            )
            # Last debit may leave remaining==0 — still need an eval round.
            if session.tools_used >= session.max_tools:
                await_conclusion = True

        if concluded:
            break

        if conclude_only:
            stop_reason = "model did not conclude after no-progress stop"
            break
        # Soft-blocked / gate-rejected calls do not debit MCP and must not
        # alone trip the two-empty-batch stop while budget remains — that
        # prematurely ends digs (e.g. run evpn fail → blocked evpn evi)
        # before get_device_config / bgp l2vpn evpn rd recovery.
        batch_tool_msgs = [
            m
            for m in messages[batch_message_start:]
            if m.get("role") == "tool"
        ]
        soft_block_only = bool(batch_tool_msgs) and not batch_responses
        if soft_block_only and blocked_unavailable_rt_path and (
            _partial_has_l2sts_rt_ladder(partial_results)
        ):
            # nso21: ladder already tried; late run evpn soft-block → conclude.
            # Config/BGP "returned" ≠ sufficient service-specific evidence.
            conclude_only = True
            force_conclude_choice = True
            stop_reason = (
                "unavailable RT evidence path after recovery ladder "
                "(run evpn blocked; config/BGP returned but may lack "
                "service-specific proof)"
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "The RT/config path via 'run evpn' is unavailable. "
                        "get_device_config and bgp l2vpn evpn checks already "
                        "returned — that does NOT mean they supplied "
                        "sufficient evidence for this service (successful "
                        "tool return ≠ numeric/effective RT, bidirectional "
                        "MAC/EVPN install, or PE-side readiness proof). "
                        "Call conclude_dataplane now. Prefer "
                        "dataplane_status=unknown when service-specific "
                        "proof is still missing; name the missing check "
                        "explicitly (e.g. numeric/effective RT text, "
                        "unverified MAC/EVPN direction). Do not claim up "
                        "from config/BGP return alone. Do not retry run "
                        "evpn / show evpn / evpn evi, and do not fish with "
                        "AC interface shows for RT proof."
                    ),
                }
            )
            _log(
                "soft-block run evpn after RT ladder — forcing conclude "
                "(allow unknown; name missing service-specific check)"
            )
        elif soft_block_only:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Rejected/forbidden shows do not count as investigation "
                        "progress and do not exhaust the tool budget. If "
                        "get_device_config / 'bgp l2vpn evpn summary|rd' / BD "
                        "forwarding are not done yet, do those next (at most "
                        "once each). If they already returned and "
                        "service-specific proof (numeric/effective RT, "
                        "bidirectional MAC/EVPN install) is still missing, "
                        "call conclude_dataplane with dataplane_status="
                        "unknown and name that missing check — a successful "
                        "tool return is not sufficient evidence by itself. "
                        "Do not keep retrying rejected paths."
                    ),
                }
            )
            _log(
                "soft-block-only batch — continue (budget remains; "
                "not counting as no-progress)"
            )
        elif batch_tool_msgs:
            if progress.observe(batch_responses):
                conclude_only = True
                stop_reason = "two batches without new successful tool output"
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Two consecutive batches produced only errors, empty, "
                            "or repeated output. Call conclude_dataplane now. "
                            "Preserve supported observations; report unknown or "
                            "unresolved cause where evidence is insufficient."
                        ),
                    }
                )

        # Nudge after last productive round if still probing
        if round_i == max_rounds - 2 and not concluded:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Budget nearly exhausted. Call conclude_dataplane now "
                        "for THIS service only (unknown if unsure)."
                    ),
                }
            )

    if not concluded:
        if investigation.stop_reason == "no_conclusion":
            if session.tools_used >= session.max_tools:
                investigation.stop_reason = "tool_limit"
            elif investigation.rounds >= max_rounds:
                investigation.stop_reason = "round_limit"
        finding = _incomplete_verify_finding(record, stop_reason=stop_reason)
        finding["partial_tool_results"] = partial_results
        if partial_results:
            finding["observed"] = str(finding.get("observed", "")) + (
                f" Held {len(partial_results)} tool responses in this run's "
                "in-memory case only (partial_tool_results) — not written to "
                "disk; observations for this report, not a completed diagnosis."
            )
        _record_incomplete_dataplane_verify(
            case,
            record,
            finding,
            evidence_ids=evidence_ids_after(case, ev_watermark),
        )
        _log(f"incomplete: unknown for {name} (not explained)")
        concluded = True

    _ = session.tools_used - before
    return concluded


async def run_dataplane_verify_phase(
    client: Any,
    settings: Settings,
    case: CaseFile,
    *,
    device_names: set[str],
    skip_llm: bool = False,
    max_services: int | None = None,
    max_per_category: int | None = None,
    openai_client: Any | None = None,
    suspicious_only: bool = False,
    explicit_service: bool = False,
    category_rotate_seed: str | None = None,
) -> None:
    """LLM dataplane verify for selected service instances.

    Default: one best instance per service type that has a typed
    ``dataplane_agent_<type>.txt`` prompt — including healthy / basic_passed
    instances (basic checks do not skip the typed-category sample). If only
    one typed category is present, verify up to two instances of it. Pass
    ``max_per_category`` for an even sample per typed prompt, and/or
    ``max_services`` as a total cap. Soft-error live L2 is preferred within
    the cap, not force-included beyond it. Categories are interleaved
    (round-robin) and optionally rotated by ``category_rotate_seed``.
    """
    if skip_llm:
        return
    from agent.llm_budget import (
        ProviderBudgetExceeded,
        case_llm_halted,
        mark_case_llm_halt,
    )

    # None → one-per-typed-category (two if only one type). Explicit 0 → skip.
    limit: int | None
    if max_services is not None:
        limit = max(0, int(max_services))
    else:
        limit = None
    per_category: int | None
    if max_per_category is not None:
        per_category = max(0, int(max_per_category))
    else:
        per_category = None
    candidates = select_dataplane_candidates(
        case,
        limit=limit,
        per_category=per_category,
        suspicious_only=suspicious_only,
        explicit_service=explicit_service,
        one_per_typed_category=True,
        category_rotate_seed=category_rotate_seed,
    )
    apply_coverage_after_candidate_select(case, candidates)
    if not candidates:
        _log(
            "no candidates"
            + (
                " (suspicious_only; basic checks only)"
                if suspicious_only
                else " (need system=up, dataplane=not_checked, typed prompt)"
            )
        )
        return
    typed = sorted(list_typed_dataplane_prompt_types())
    cap_bits: list[str] = []
    if per_category is not None:
        cap_bits.append(f"per_category={per_category}")
    if limit is not None:
        cap_bits.append(f"cap={limit}")
    if category_rotate_seed:
        cap_bits.append(f"rotate_seed={category_rotate_seed}")
    _log(
        f"candidates={len(candidates)} "
        f"(typed_category_select; types={typed}"
        + (f"; {'; '.join(cap_bits)}" if cap_bits else "")
        + ("; suspicious_only" if suspicious_only else "")
        + "): "
        + ", ".join(
            f"{r.get('service_type')}:{r.get('name')}" for _e, r in candidates
        )
    )

    # Cap uses Budget.max_dataplane_tools (CLI --max-dataplane-tools, default 40).
    tools_cap = dataplane_tools_cap(case.budget)
    if tools_cap <= 0:
        _log("tools_cap=0 — skipping dataplane LLM verify")
        for _ev, rec in candidates:
            name = str(rec.get("name") or "").strip()
            if name:
                case.service_coverage[name] = "budget_skipped"
        return
    _log(f"tools_cap={tools_cap} rounds_cap={_DATAPLANE_MAX_ROUNDS_CAP}")
    touched_ev: set[int] = set()
    halted_at: int | None = None
    for idx, (ev, rec) in enumerate(candidates):
        if case_llm_halted(case):
            halted_at = idx
            break
        session = DrillSession(
            max_tools=tools_cap,
            issue_id=None,
            issue_edge_id=str(rec.get("name") or ""),
        )
        try:
            ok = await llm_dataplane_verify_one(
                client,
                settings,
                case,
                record=rec,
                device_names=device_names,
                session=session,
                openai_client=openai_client,
            )
        except ProviderBudgetExceeded as exc:
            mark_case_llm_halt(case, str(exc))
            _log(
                "provider LLM budget exceeded — preserving completed digs; "
                f"skipping remaining {len(candidates) - idx - 1} candidate(s)"
            )
            halted_at = idx + 1
            break
        if ok:
            touched_ev.add(id(ev))
        else:
            _log(f"no status recorded for {rec.get('name')}")

    if halted_at is not None:
        for _ev, rec in candidates[halted_at:]:
            name = str(rec.get("name") or "").strip()
            if not name:
                continue
            # Keep completed digs; only mark not-yet-started remainder.
            if case.service_coverage.get(name) in {
                "investigated",
                "unresolved",
            }:
                continue
            # Skip if a diagnosis already exists for this service.
            has_dx = any(
                d.get("kind") == "dataplane"
                and isinstance(d.get("subject"), dict)
                and str(d["subject"].get("name") or "") == name
                for d in case.diagnoses
            )
            if has_dx:
                continue
            case.service_coverage[name] = "llm_budget_exceeded"

    for ev, _rec in candidates:
        if id(ev) in touched_ev:
            _refresh_spine_counts(ev)
            extra = _spine_extra(ev) or {}
            services = extra.get("services")
            if isinstance(services, dict):
                _open_issues_for_updated_services(case, services)
    update_coverage_from_diagnoses(case)
