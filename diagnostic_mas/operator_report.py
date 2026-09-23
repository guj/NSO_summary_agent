"""Deterministic operator-facing report sections (devices / services / follow-up)."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from diagnostic_mas.case import CaseFile
from diagnostic_mas.device_health import (
    fleet_maps_from_case,
    format_detailed_devices_section,
    services_from_case,
    topology_from_case,
)
from diagnostic_mas.roles.summary import scrub_internal_ids
from nso_report.executive import build_device_health_rows

_MAPPING_CODES = frozenset(
    {
        "unknown_neighbor_address",
        "unknown_neighbor_system_id",
        "device_live_unreachable",
    }
)

# Labels when dig concludes dataplane=up (PE-side readiness, not E2E delivery).
RESULT_PE_SIDE_PASSED = "Passed PE-side readiness checks."
RESULT_INCOMPLETE_QUERY = (
    "Incomplete verification — blocked by query/object failure"
)
RESULT_INCOMPLETE_NEEDS_TRAFFIC = (
    "Incomplete verification — requires customer-generated traffic"
)
RESULT_INCOMPLETE_GENERIC = "Incomplete verification"
UNCERTAINTY_DELIVERY_UNVERIFIED = "Customer traffic delivery was not tested."
NEXT_SCOPED_REACHABILITY = (
    "Obtain a known customer endpoint and perform a scoped reachability "
    "test; correlate any failure with forwarding evidence."
)
NEXT_CORRECTED_QUERY = (
    "Fix the device query/object identity (exact BG:BD), then re-check "
    "BD-scoped MAC/forwarding — do not substitute another global BGP summary."
)
NEXT_CUSTOMER_TRAFFIC = (
    "Obtain a known customer endpoint on each side (addresses + attachment) "
    "and access for a scoped L2 exchange; PE-loopback reachability does not "
    "test the customer L2 path."
)


def format_duration(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    total = int(round(seconds))
    mins, secs = divmod(total, 60)
    if mins <= 0:
        return f"{secs} sec"
    return f"{mins} min {secs} sec"


def _issues_for_device(case: CaseFile, device: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for issue in case.issues:
        if not isinstance(issue, dict):
            continue
        devices = issue.get("devices") or []
        if device in devices:
            out.append(issue)
            continue
        msg = str(issue.get("message") or "")
        if device in msg:
            out.append(issue)
    return out


def _mapping_attention(issue: dict[str, Any]) -> str | None:
    code = str(issue.get("code") or "")
    msg = scrub_internal_ids(str(issue.get("message") or "").strip())
    if code == "unknown_neighbor_address":
        return (
            f"BGP neighbor could not be mapped to NSO inventory. {msg}".strip()
            if msg
            else "BGP neighbor could not be mapped to NSO inventory."
        )
    if code == "unknown_neighbor_system_id":
        return (
            f"IS-IS neighbor could not be mapped. {msg}".strip()
            if msg
            else "IS-IS neighbor could not be mapped."
        )
    if code == "device_live_unreachable":
        return (
            msg
            if msg
            else (
                "Device live query timed out or failed this run; NSO config "
                "may still be present; further live MCP skipped."
            )
        )
    return None


def _strip_detail_hint(notes: str) -> str:
    text = (notes or "").strip()
    if not text:
        return ""
    return re.sub(
        r"\s*[—–-]\s*(?:see Detailed Analysis|use --full)\.?\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def _hardware_observations(entry: dict[str, Any] | None) -> list[str]:
    """Concrete hardware-check observations (not 'fault confirmed')."""
    if not isinstance(entry, dict) or entry.get("error"):
        return []
    bits: list[str] = []
    for key, label in (
        ("temperature", "temperature sensor not ok"),
        ("fans", "fan status not ok"),
        ("power", "power supply status not ok"),
    ):
        rows = entry.get(key) if isinstance(entry.get(key), list) else []
        if any(isinstance(x, dict) and x.get("ok") is False for x in rows):
            bits.append(label)
    drops = 0
    cps = (
        entry.get("control_plane")
        if isinstance(entry.get("control_plane"), list)
        else []
    )
    for row in cps:
        if isinstance(row, dict):
            try:
                drops += int(row.get("dropped") or 0)
            except (TypeError, ValueError):
                pass
    if drops > 0:
        bits.append(f"control-plane drop counter={drops:,} recorded")
    return bits


def _inventory_observations(notes: str) -> list[str]:
    """Map compact Device Health notes to operator-facing observations."""
    text = _strip_detail_hint(notes)
    if not text:
        return []
    bits: list[str] = []
    lower = text.lower()
    if "mapping unknown" in lower:
        bits.append("NSO↔device interface mapping unconfirmed")
    if "interface up/down" in lower:
        bits.append("interface admin/oper up/down observed")
    # Any residual note text not covered above
    residual = text
    for token in ("Mapping unknown", "Interface up/down"):
        residual = residual.replace(token, "")
    residual = re.sub(r"\s*,\s*", ", ", residual).strip(" ,")
    if residual:
        bits.append(residual)
    return bits


def _health_prose(
    row: dict[str, Any],
    *,
    full: bool = False,
    hardware_entry: dict[str, Any] | None = None,
) -> str:
    iface = str(row.get("interfaces") or "—")
    hw = str(row.get("hardware") or "Unavailable")
    iface_l = iface.lower()
    hw_l = hw.lower()
    # Labels from build_device_health_rows use "Healthy" (and sometimes OK/Up).
    healthy_iface = iface_l in {"ok", "healthy", "up", "—", "-"}
    healthy_hw = hw_l in {"ok", "healthy", "unavailable", "—", "-"}
    observations = _inventory_observations(str(row.get("notes") or ""))
    observations.extend(_hardware_observations(hardware_entry))
    # Deduplicate while preserving order
    seen: set[str] = set()
    obs_unique: list[str] = []
    for bit in observations:
        if bit in seen:
            continue
        seen.add(bit)
        obs_unique.append(bit)

    if healthy_iface and healthy_hw and not obs_unique:
        if hw_l in {"unavailable", "—", "-"} and iface in {"—", "-"}:
            return "Interface and hardware status not collected for this run"
        return "Interface and hardware checks reported healthy"

    if obs_unique or not healthy_iface or not healthy_hw:
        hint = "see Detailed Analysis" if full else "use --full"
        obs_txt = (
            "; ".join(obs_unique)
            if obs_unique
            else "review labels present without a more specific observation string"
        )
        return (
            f"{obs_txt}. "
            f"Labels: interfaces={iface}; hardware={hw}. "
            "Review is a triage flag from this run's checks — do not treat it "
            f"as a confirmed hardware fault ({hint})."
        )
    return f"Interfaces={iface}; hardware={hw}"


def _device_has_health_review(row: dict[str, Any]) -> bool:
    """True when Interfaces/Hardware notes or non-healthy review labels apply."""
    iface = str(row.get("interfaces") or "—").lower()
    hw = str(row.get("hardware") or "Unavailable").lower()
    notes = _strip_detail_hint(str(row.get("notes") or ""))
    healthy_iface = iface in {"ok", "healthy", "up", "—", "-"}
    healthy_hw = hw in {"ok", "healthy", "unavailable", "—", "-"}
    return bool(notes) or not healthy_iface or not healthy_hw


def _device_needs_inventory_followup(row: dict[str, Any]) -> bool:
    """True only for real inventory/mapping signals — not drop-counter Review."""
    notes = _strip_detail_hint(str(row.get("notes") or ""))
    if not notes:
        return False
    lower = notes.lower()
    return "mapping unknown" in lower or "interface up/down" in lower


def _sync_prose(sync: str) -> str:
    s = (sync or "—").strip()
    if s.lower() in {"in-sync", "in sync", "synced", "true"}:
        return "In sync"
    if s in {"—", "-", ""}:
        return "Not collected"
    return s


def _routing_count_prose(count: str) -> str:
    """Format BGP/IS-IS peer count; unknown → N/A (no trailing 'up')."""
    s = (count or "—").strip()
    if s in {"—", "-", "", "n/a", "N/A"}:
        return "N/A"
    return f"{s} up"


_BGP_EDGE_ID_RE = re.compile(
    r"^bgp:(?P<a>\d+\.\d+\.\d+\.\d+):(?P<b>\d+\.\d+\.\d+\.\d+):"
    r"(?P<dev_a>[^:]+):(?P<dev_b>.+)$"
)
_BGP_INCOMPLETE_CODES = frozenset(
    {"configured_no_session", "missing_reverse_session"}
)


def _devices_from_bgp_edge_id(edge_id: str) -> tuple[str, str] | None:
    m = _BGP_EDGE_ID_RE.match((edge_id or "").strip())
    if not m:
        return None
    return m.group("dev_a"), m.group("dev_b")


def _issue_touches_device(issue: dict[str, Any], device: str) -> bool:
    devices = issue.get("devices") or []
    if device in devices:
        return True
    edge_id = str(issue.get("edge_id") or "")
    pair = _devices_from_bgp_edge_id(edge_id)
    if pair and device in pair:
        return True
    msg = str(issue.get("message") or "")
    return device in msg


def _bgp_incomplete_peer_issues(
    case: CaseFile, device: str
) -> list[dict[str, Any]]:
    """Spine BGP issues where live bidirectional verification was incomplete."""
    out: list[dict[str, Any]] = []
    for issue in case.issues:
        if not isinstance(issue, dict):
            continue
        if str(issue.get("code") or "") not in _BGP_INCOMPLETE_CODES:
            continue
        if not _issue_touches_device(issue, device):
            continue
        out.append(issue)
    return out


def _peer_from_incomplete_issue(issue: dict[str, Any], device: str) -> str | None:
    pair = _devices_from_bgp_edge_id(str(issue.get("edge_id") or ""))
    if pair:
        a, b = pair
        if device == a:
            return b
        if device == b:
            return a
    msg = str(issue.get("message") or "")
    # "star-data-sw … ↔ atla-data-sw …"
    names = re.findall(r"\b([a-z0-9-]+-data-sw)\b", msg, flags=re.IGNORECASE)
    peers = [n for n in names if n != device]
    return peers[0] if peers else None


def _drill_findings_by_issue_key(case: CaseFile) -> dict[str, dict[str, Any]]:
    """Map issue id / edge_id → latest drill_finding payload."""
    out: dict[str, dict[str, Any]] = {}
    for ev in case.evidence:
        if ev.get("kind") != "drill_finding":
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        for field in ("issue_id", "issue_edge_id"):
            key = payload.get(field)
            if key is not None and str(key).strip():
                out[str(key).strip()] = payload
    return out


def _drill_finding_is_reachable_side_up(payload: dict[str, Any]) -> bool:
    """True when drill concludes BGP is up on the reachable endpoint."""
    observed = str(payload.get("observed") or "").lower()
    cause = str(payload.get("cause") or "").lower()
    text = f"{observed} {cause}".strip()
    if not text:
        return False
    # Strong positive markers from BGP drills against quarantined peers.
    positive = (
        "false positive" in text
        or "no real bgp fault" in text
        or "not a real routing fault" in text
        or "reachable side" in text
        or "is established" in text
        or "session is established" in text
        or ("establish" in text and "quarantine" in text)
    )
    # Only treat as negative when the conclusion itself is a confirmed down.
    negative = (
        "session is down" in text
        or "confirmed down" in text
        or "genuinely not established" in text
        or text.startswith("down:")
    )
    return positive and not negative


def _bgp_routing_qualification(
    case: CaseFile, device: str, bgp_count: str
) -> tuple[str, str | None]:
    """Return (bgp prose, optional drill follow-up line).

    Distinguishes spine ``0/N`` from incomplete peer live checks vs later
    drill evidence that the reachable side is Established.
    """
    base = _routing_count_prose(bgp_count)
    incomplete = _bgp_incomplete_peer_issues(case, device)
    if not incomplete:
        return base, None

    findings = _drill_findings_by_issue_key(case)
    up_peers: list[str] = []
    for issue in incomplete:
        keys = [
            str(x).strip()
            for x in (issue.get("id"), issue.get("edge_id"))
            if x is not None and str(x).strip()
        ]
        payload = next((findings[k] for k in keys if k in findings), None)
        if payload is None or not _drill_finding_is_reachable_side_up(payload):
            continue
        peer = _peer_from_incomplete_issue(issue, device)
        if peer and peer not in up_peers:
            up_peers.append(peer)

    # Qualify spine count whenever bidirectional live verification was incomplete.
    if base != "N/A":
        bgp_prose = f"{base} (initial; peer live checks unavailable)"
    else:
        bgp_prose = base

    drill_line: str | None = None
    if up_peers:
        peers = ", ".join(f"`{p}`" for p in up_peers)
        drill_line = (
            f"**Drill:** BGP Established on `{device}` toward {peers} "
            "(reachable side only; peer devices not live-checked this run)"
        )
    return bgp_prose, drill_line


def _brief_device_sync_issue(device: str, result: str) -> str | None:
    """One short clause for a non-in-sync device_sync value."""
    r = (result or "").strip()
    if not r:
        return None
    lowered = r.lower()
    if lowered == "in-sync":
        return None
    if "timed out" in lowered or "timeout" in lowered:
        return f"{device}: sync check timed out"
    if "unreachable" in lowered:
        return f"{device} unreachable"
    if lowered.startswith("error"):
        return f"{device}: sync check error"
    if lowered in {"out-of-sync", "out of sync"}:
        return f"{device} out-of-sync"
    if lowered == "locked":
        return f"{device} sync locked"
    if lowered == "unknown":
        return f"{device} sync unknown"
    return f"{device}: {r}" if len(r) <= 48 else f"{device}: {r[:45]}…"


def _collection_status_reason(rec: dict[str, Any]) -> str | None:
    """Short why collection marked non-up; None when up or nothing useful."""
    status = str(rec.get("status") or rec.get("system_status") or "").lower()
    if status in {"up", "ok", ""}:
        return None

    bits: list[str] = []
    device_sync = rec.get("device_sync")
    if isinstance(device_sync, dict):
        for device in sorted(device_sync):
            clause = _brief_device_sync_issue(str(device), str(device_sync.get(device) or ""))
            if clause:
                bits.append(clause)
            if len(bits) >= 2:
                break

    if rec.get("in_sync") is False and not any("out-of-sync" in b for b in bits):
        bits.insert(0, "service intent out-of-sync")

    sync_error = rec.get("sync_error")
    if sync_error and not bits:
        err = str(sync_error).strip()
        lowered = err.lower()
        if "timed out" in lowered or "timeout" in lowered:
            bits.append("service sync check timed out")
        else:
            bits.append(
                "service sync error"
                if len(err) > 48
                else f"service sync error: {err}"
            )

    live = rec.get("live_l2") if isinstance(rec.get("live_l2"), dict) else {}
    live_summary = str(live.get("summary") or "").lower()
    if live_summary in {"down", "degraded"} and not bits:
        bits.append(f"live L2 {live_summary}")

    if not bits:
        return None
    return "; ".join(bits[:2])


def _collection_verification_problem(rec: dict[str, Any]) -> bool:
    """True when collection 'down' is a sync/query verification miss, not live L2."""
    reason = (_collection_status_reason(rec) or "").lower()
    if not reason:
        return False
    if "live l2" in reason:
        return False
    return any(
        token in reason
        for token in (
            "sync check timed out",
            "sync check error",
            "sync locked",
            "sync unknown",
            "service sync check timed out",
            "service sync error",
            "unreachable",
        )
    )


def _device_sync_verification_failure(result: str) -> bool:
    """True when a per-endpoint sync value is a query/verification miss."""
    lowered = (result or "").strip().lower()
    if not lowered or lowered == "in-sync":
        return False
    if lowered in {"out-of-sync", "out of sync", "not-in-sync"}:
        return False
    return True


def _failing_verification_endpoints(rec: dict[str, Any]) -> tuple[str, ...]:
    """Endpoint devices whose sync check failed as a verification gap."""
    device_sync = rec.get("device_sync")
    if not isinstance(device_sync, dict):
        return ()
    bad = [
        str(device)
        for device, result in device_sync.items()
        if device and _device_sync_verification_failure(str(result or ""))
    ]
    return tuple(sorted(bad))


def _iter_service_records(case: CaseFile) -> list[tuple[str, dict[str, Any]]]:
    """(name, record) for flat service map entries."""
    out: list[tuple[str, dict[str, Any]]] = []
    for key, rec in services_from_case(case).items():
        if not isinstance(rec, dict):
            continue
        name = str(rec.get("name") or key.split("/", 1)[-1]).strip()
        if name:
            out.append((name, rec))
    return out


def _service_status_counts(case: CaseFile) -> dict[str, int]:
    counts = {"up": 0, "down": 0, "degraded": 0, "unknown": 0}
    for _name, rec in _iter_service_records(case):
        status = str(rec.get("status") or rec.get("system_status") or "unknown").lower()
        if status in counts:
            counts[status] += 1
        else:
            counts["unknown"] += 1
    return counts


def _verification_unknown_by_endpoints(
    case: CaseFile,
) -> list[tuple[tuple[str, ...], int]]:
    """Unknown-from-sync services grouped by failing endpoint sync-check set.

    Counts sync-layer unknowns / endpoint query gaps only — not dataplane-only
    overall unknown when ``system_status`` stayed up.

    Returns (endpoint_tuple, count) sorted by count descending, then device name.
    Empty endpoint tuple means sync unknown without per-device sync failure detail.
    """
    groups: Counter[tuple[str, ...]] = Counter()
    for _name, rec in _iter_service_records(case):
        sys = str(rec.get("system_status") or "").lower()
        status = str(rec.get("status") or "").lower()
        eps = _failing_verification_endpoints(rec)
        if eps:
            groups[eps] += 1
            continue
        if sys == "unknown" or (
            status == "unknown" and _collection_verification_problem(rec)
        ):
            groups[()] += 1
    return sorted(groups.items(), key=lambda item: (-item[1], item[0]))


def _verification_unknown_involvement_by_device(case: CaseFile) -> Counter[str]:
    """Per-device involvement in verification-unknown services.

    A multi-endpoint service increments each failing endpoint once. Counts
    overlap across devices and must not be summed to get distinct services.
    """
    counts: Counter[str] = Counter()
    for _name, rec in _iter_service_records(case):
        eps = _failing_verification_endpoints(rec)
        if not eps:
            continue
        for device in eps:
            counts[str(device)] += 1
    return counts


def _verification_unknown_service_names(case: CaseFile) -> set[str]:
    """Service names included in endpoint-sync unknown grouping."""
    names: set[str] = set()
    for name, rec in _iter_service_records(case):
        sys = str(rec.get("system_status") or "").lower()
        status = str(rec.get("status") or "").lower()
        eps = _failing_verification_endpoints(rec)
        if eps or sys == "unknown" or (
            status == "unknown" and _collection_verification_problem(rec)
        ):
            names.add(name)
    return names


def _format_endpoint_group_label(endpoints: tuple[str, ...]) -> str:
    if not endpoints:
        return "service sync check"
    if len(endpoints) == 1:
        return f"`{endpoints[0]}`"
    return " + ".join(f"`{d}`" for d in endpoints)


def _format_verification_unknown_summary(
    groups: list[tuple[tuple[str, ...], int]],
    *,
    total: int | None = None,
    max_groups: int = 5,
) -> str | None:
    """Compact prose for Result about endpoint-check unknowns."""
    if not groups:
        return None
    n = int(total if total is not None else sum(c for _eps, c in groups))
    if n <= 0:
        return None
    top = groups[: max(1, max_groups)]
    bits = [
        f"{_format_endpoint_group_label(eps)} ({count})" for eps, count in top
    ]
    more = len(groups) - len(top)
    detail = ", ".join(bits)
    if more > 0:
        detail += f", +{more} other endpoint group{'s' if more != 1 else ''}"
    return (
        f"{n} service{'s' if n != 1 else ''} unknown from endpoint sync/query "
        f"gaps (not confirmed down): {detail}"
    )


def _sync_failure_reason_kind(result: str) -> str:
    """Normalize a device_sync failure into a short reason label."""
    lowered = (result or "").strip().lower()
    if not lowered or lowered == "in-sync":
        return "sync check incomplete"
    if "timed out" in lowered or "timeout" in lowered:
        return "sync check timed out"
    if "unreachable" in lowered:
        return "unreachable"
    if lowered == "locked":
        return "sync locked"
    if lowered == "unknown":
        return "sync unknown"
    if lowered.startswith("error") or "error" in lowered:
        return "sync check error"
    return "sync check incomplete"


def _incomplete_checks_by_endpoint_reason(
    case: CaseFile,
) -> list[tuple[tuple[str, ...], str, int]]:
    """Group sync-layer unknowns by (endpoints, reason) for compact Services."""
    groups: Counter[tuple[tuple[str, ...], str]] = Counter()
    for _name, rec in _iter_service_records(case):
        sys = str(rec.get("system_status") or "").lower()
        status = str(rec.get("status") or "").lower()
        device_sync = rec.get("device_sync")
        eps = _failing_verification_endpoints(rec)
        if eps:
            reasons: list[str] = []
            if isinstance(device_sync, dict):
                for device in eps:
                    reasons.append(
                        _sync_failure_reason_kind(str(device_sync.get(device) or ""))
                    )
            reason = "; ".join(dict.fromkeys(reasons)) or "sync check incomplete"
            groups[(eps, reason)] += 1
            continue
        if sys == "unknown" or (
            status == "unknown" and _collection_verification_problem(rec)
        ):
            groups[((), "service sync incomplete")] += 1
    rows = sorted(
        groups.items(), key=lambda item: (-item[1], item[0][0], item[0][1])
    )
    return [(eps, reason, count) for (eps, reason), count in rows]


def format_incomplete_checks_grouped(case: CaseFile) -> list[str]:
    """Compact bullet list of incomplete collection checks by endpoint/reason."""
    rows = _incomplete_checks_by_endpoint_reason(case)
    if not rows:
        return []
    lines = ["### Incomplete collection checks", ""]
    for endpoints, reason, count in rows:
        label = _format_endpoint_group_label(endpoints)
        lines.append(
            f"- {label} · {reason} — {count} service"
            f"{'s' if count != 1 else ''} (not confirmed down)"
        )
    lines.append("")
    return lines


def _service_instance_interesting(
    name: str,
    rec: dict[str, Any],
    *,
    has_dataplane: bool,
    has_service_issue: bool,
) -> bool:
    """True when compact mode should still emit a per-instance section."""
    if has_dataplane or has_service_issue:
        return True
    status = str(rec.get("status") or "").lower()
    sys = str(rec.get("system_status") or "").lower()
    if status in {"down", "degraded"} and not _collection_verification_problem(rec):
        return True
    if sys in {"down", "degraded"} and not _collection_verification_problem(rec):
        return True
    return False


def _coverage_label(code: str) -> str:
    return {
        "basic_passed": "Basic checks passed",
        "investigated": "Additional investigation completed",
        "unresolved": "Unresolved after investigation",
        "budget_skipped": "Not investigated (budget limit)",
        "llm_budget_exceeded": (
            "Not investigated (provider LLM spend budget exceeded)"
        ),
        "collection_concluded": (
            "Not re-verified (collection verification incomplete — "
            "not a confirmed forwarding fault)"
        ),
        "needs_investigation": "Pending investigation",
        "category_peer_skipped": (
            "Not selected (typed-category dataplane sample is another instance)"
        ),
    }.get(code, code)


def _service_coverage_code(case: CaseFile, name: str, rec: dict[str, Any]) -> str:
    """Coverage for report; never claim basic_passed when collection is unhealthy."""
    from diagnostic_mas.dataplane_verify import _collection_already_unhealthy

    cov = case.service_coverage.get(name) if case.service_coverage else None
    status = str(rec.get("status") or rec.get("system_status") or "").lower()
    unhealthy = _collection_already_unhealthy(rec)
    verification = _collection_verification_problem(rec)
    # Hard rule: non-up / sync-timeout downs are never "Basic checks passed".
    if unhealthy or verification or status in {"down", "degraded", "unknown"}:
        if cov in {None, "basic_passed", "needs_investigation"}:
            return "collection_concluded"
    if cov == "basic_passed" and status not in {"up", "ok", ""}:
        return "collection_concluded"
    if cov:
        return str(cov)
    if status in {"up", "ok"}:
        return "basic_passed"
    return "needs_investigation"


def _device_rows(case: CaseFile) -> list[dict[str, Any]]:
    topology = topology_from_case(case)
    fleet_sync, hardware_health, _system = fleet_maps_from_case(case)
    rows = build_device_health_rows(
        topology, fleet_sync, hardware_health=hardware_health
    )
    focus = list(case.focus_devices or []) or list(case.device_names or [])
    seen = {str(r.get("device")) for r in rows}
    for name in focus:
        if name in seen:
            continue
        rows.append(
            {
                "device": name,
                "sync": "—",
                "interfaces": "—",
                "hardware": "Unavailable",
                "bgp": "—",
                "isis": "—",
                "routes": "—",
                "notes": "",
            }
        )
        seen.add(name)
    if case.focus_devices:
        allow = set(case.focus_devices)
        rows = [r for r in rows if str(r.get("device")) in allow]
    rows.sort(key=lambda r: str(r.get("device") or ""))
    return rows


def format_devices_operator(case: CaseFile, *, full: bool = False) -> list[str]:
    """Per-device operator sections."""
    rows = _device_rows(case)
    if not rows:
        return ["(none — no device names or spine data)"]
    _fleet_sync, hardware_health, _system = fleet_maps_from_case(case)
    hw_map = hardware_health if isinstance(hardware_health, dict) else {}

    lines: list[str] = []
    for row in rows:
        device = str(row.get("device") or "?")
        hw_entry = hw_map.get(device) if isinstance(hw_map.get(device), dict) else None
        lines.append(f"### {device}")
        lines.append("")
        lines.append(f"**NSO sync:** {_sync_prose(str(row.get('sync') or '—'))}")
        lines.append(
            f"**Health:** {_health_prose(row, full=full, hardware_entry=hw_entry)}"
        )
        bgp_prose, drill_line = _bgp_routing_qualification(
            case, device, str(row.get("bgp") or "—")
        )
        isis = _routing_count_prose(str(row.get("isis") or "—"))
        lines.append(f"**Routing:** BGP {bgp_prose} · IS-IS {isis}")
        if drill_line:
            lines.append(drill_line)
        lines.append("")

        attention: list[str] = []
        for issue in _issues_for_device(case, device):
            mapped = _mapping_attention(issue)
            if mapped:
                attention.append(mapped)
            elif str(issue.get("code") or "") not in _MAPPING_CODES:
                # Keep high-severity open service issues off device Attention
                continue
        # Health prose already states observations + triage caveat; no duplicate
        # "unexplained flags" Attention bullet.
        if attention:
            if len(attention) == 1:
                lines.append(f"**Attention:** {attention[0]}")
            else:
                lines.append("**Attention:**")
                lines.append("")
                for item in attention:
                    lines.append(f"- {item}")
            lines.append("")
    return lines


def _dataplane_by_name(case: CaseFile) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for dx in case.diagnoses:
        if dx.get("kind") != "dataplane":
            continue
        subject = dx.get("subject") if isinstance(dx.get("subject"), dict) else {}
        name = subject.get("name") or dx.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        out[name.strip()] = {
            "name": name.strip(),
            "service_type": subject.get("service_type") or dx.get("service_type"),
            "dataplane_status": dx.get("status") or dx.get("dataplane_status"),
            "observed": dx.get("observed"),
            "cause": dx.get("cause"),
            "fix_suggestion": dx.get("fix_suggestion"),
            "complete": dx.get("complete", True),
            "source": dx.get("source") or "llm",
            "verification_gap": dx.get("verification_gap"),
            "investigation": dx.get("investigation"),
        }
    if out:
        return out
    for ev in case.evidence:
        if ev.get("kind") not in {"dataplane_finding", "dataplane_incomplete"}:
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        name = payload.get("name") or payload.get("service_name")
        if not isinstance(name, str) or not name.strip():
            continue
        out[name.strip()] = {
            "name": name.strip(),
            "service_type": payload.get("service_type"),
            "dataplane_status": payload.get("dataplane_status"),
            "observed": payload.get("observed"),
            "verification_gap": payload.get("verification_gap"),
            "investigation": payload.get("investigation"),
            "cause": payload.get("cause"),
            "fix_suggestion": payload.get("fix_suggestion"),
            "complete": payload.get(
                "complete", ev.get("kind") != "dataplane_incomplete"
            ),
            "source": payload.get("source") or "llm",
        }
    return out


def _service_issue_for(case: CaseFile, name: str) -> dict[str, Any] | None:
    for issue in case.issues:
        if issue.get("layer") != "services":
            continue
        if issue.get("edge_id") == name:
            return issue
    return None


def _endpoint_summary(rec: dict[str, Any]) -> str | None:
    devices = [str(d) for d in (rec.get("devices") or []) if isinstance(d, str)]
    live = rec.get("live_l2") if isinstance(rec.get("live_l2"), dict) else {}
    eps = live.get("endpoints") if isinstance(live.get("endpoints"), list) else []
    ep_devices = [
        str(ep.get("device"))
        for ep in eps
        if isinstance(ep, dict) and ep.get("device")
    ]
    names = ep_devices or devices
    if len(names) >= 2:
        return f"`{names[0]}` ↔ `{names[1]}`"
    if len(names) == 1:
        return f"`{names[0]}`"
    return None


def _live_l2_body(rec: dict[str, Any]) -> list[str]:
    live = rec.get("live_l2") if isinstance(rec.get("live_l2"), dict) else {}
    eps = live.get("endpoints") if isinstance(live.get("endpoints"), list) else []
    if not eps:
        return []
    bits: list[str] = []
    ac_errors: list[str] = []
    all_up = True
    for ep in eps:
        if not isinstance(ep, dict):
            continue
        st = str(ep.get("st") or "").upper()
        err = ep.get("error")
        if err:
            ac_errors.append(str(err))
            all_up = False
        elif st and st != "UP":
            all_up = False
        ac = ep.get("ac") or "?"
        device = ep.get("device") or "?"
        xc = ep.get("xconnect")
        part = f"{device} {ac} ST={st or '?'}"
        if xc:
            part += f" xconnect `{xc}`"
        bits.append(part)
    lines: list[str] = []
    if bits:
        lines.append("Endpoints observed: " + "; ".join(bits) + ".")
    if all_up and bits:
        lines.append("Attachment interfaces reported up.")
    if ac_errors:
        lines.append(
            "Collector notes: " + ", ".join(sorted(set(ac_errors))) + "."
        )
    return lines


def _format_evidence_block(obs: str) -> list[str]:
    """Essential dig evidence beside the finding (not raw CLI dumps)."""
    text = scrub_internal_ids(str(obs or "").strip())
    if not text:
        return []
    lines = ["**Evidence:**"]
    if "\n" in text or text.lstrip().startswith("-"):
        for line in text.splitlines():
            lines.append(line)
    else:
        lines.append(f"- {text}")
    return lines


def _format_uncertainty_block(items: list[str]) -> list[str]:
    """Label remaining unverified gaps clearly."""
    cleaned = [scrub_internal_ids(str(u).strip()) for u in items if str(u).strip()]
    if not cleaned:
        return []
    if len(cleaned) == 1:
        return ["**Uncertainty:** " + cleaned[0]]
    lines = ["**Uncertainty:**"]
    for u in cleaned:
        lines.append(f"- {u}")
    return lines


def _services_to_render(
    case: CaseFile, *, services_detail: bool
) -> list[tuple[str, dict[str, Any]]]:
    services = services_from_case(case)
    dp = _dataplane_by_name(case)
    keys: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            keys.append(name)

    if services_detail:
        # Full instance list (service-focus / --services-detail).
        if case.service_coverage:
            for name in case.service_coverage:
                _add(name)
        for key, rec in sorted(services.items()):
            if not isinstance(rec, dict):
                continue
            name = str(rec.get("name") or key.split("/", 1)[-1])
            _add(name)
        for name in dp:
            _add(name)
    else:
        # Compact: digs, service issues, and confirmed down/degraded only.
        for name in dp:
            _add(name)
        for issue in case.issues:
            if issue.get("layer") == "services" and isinstance(
                issue.get("edge_id"), str
            ):
                _add(issue["edge_id"])
        for key, rec in services.items():
            if not isinstance(rec, dict):
                continue
            name = str(rec.get("name") or key.split("/", 1)[-1])
            if name in seen:
                continue
            if _service_instance_interesting(
                name,
                rec,
                has_dataplane=False,
                has_service_issue=False,
            ):
                _add(name)

    out: list[tuple[str, dict[str, Any]]] = []
    for name in keys:
        rec: dict[str, Any] = {}
        for key, cand in services.items():
            if not isinstance(cand, dict):
                continue
            if cand.get("name") == name or key.endswith("/" + name) or key == name:
                rec = dict(cand)
                break
        if name in dp:
            rec = {**rec, "_diagnosis": dp[name]}
        issue = _service_issue_for(case, name)
        if issue:
            rec = {**rec, "_issue": issue}
            if isinstance(issue.get("live_l2"), dict) and "live_l2" not in rec:
                rec["live_l2"] = issue["live_l2"]
        if not rec.get("service_type") and name in dp:
            rec["service_type"] = dp[name].get("service_type")
        if not rec.get("name"):
            rec["name"] = name
        out.append((name, rec))
    return out


def _incomplete_dig_stop_kind(dx: dict[str, Any]) -> str:
    """Classify incomplete dig: timeout | budget | no_conclusion | other."""
    text = f"{dx.get('cause') or ''} {dx.get('observed') or ''}".lower()
    if (
        "neither tools nor conclude" in text
        or "returned neither tools" in text
        or "no-progress stop" in text
        or ("no tools" in text and "no conclusion" in text)
    ):
        return "no_conclusion"
    if "timed out" in text or "chat request timed out" in text:
        return "timeout"
    # Legacy Observed phrasing that mixed timeout + budget — treat as timeout.
    if "timeout or dataplane budget exhausted" in text:
        return "timeout"
    if "budget" in text and (
        "exhaust" in text or "tool" in text or "nearly" in text
    ):
        return "budget"
    return "other"


def _incomplete_dig_gap_kind(dx: dict[str, Any]) -> str:
    """Classify verification gap: query_failure | needs_traffic | other.

    Used for Result / Next when the dig finished as incomplete/unknown
    (not timeout/budget/no_conclusion). Prefer explicit Diagnosis wording.
    """
    text = f"{dx.get('cause') or ''} {dx.get('observed') or ''}".lower()
    hard_query = (
        "wrong-object" in text
        or "wrong object" in text
        or "object identity" in text
        or "identity unverified" in text
        or "l2fib" in text
        or "data corruption" in text
        or "data inconsistency" in text
        or "query failure" in text
        or "cli error" in text
        or "show client" in text
        or "could not be retrieved" in text
        or "forwarding read is not trustworthy" in text
    )
    if hard_query:
        return "query_failure"
    traffic_markers = (
        "customer-generated",
        "known customer",
        "scoped traffic",
        "scoped reachability",
        "silent",
        "empty bd mac",
        "mac tables are empty",
        "zero mac",
        "0 mac",
        "no type-2",
        "no mac",
        "mac/evpn install",
        "bidirectional",
        "flood-list",
        "flood/replication",
        "delivery unverified",
        "traffic delivery",
        "customer traffic",
        "passive",
    )
    if any(m in text for m in traffic_markers):
        return "needs_traffic"
    return "other"


def _incomplete_result_line(dx: dict[str, Any]) -> str:
    """Operator Result line for incomplete / unresolved digs."""
    status = str(dx.get("dataplane_status") or "?").lower()
    structured = dx.get("verification_gap")
    if isinstance(structured, dict):
        blocker = str(structured.get("blocker") or "insufficient_evidence")
        return f"**Result:** Incomplete verification — {blocker.replace('_', ' ')} (dataplane={status}; unresolved)"
    stop = _incomplete_dig_stop_kind(dx)
    if stop in {"timeout", "budget", "no_conclusion"}:
        return (
            f"**Result:** {RESULT_INCOMPLETE_GENERIC} "
            f"(dataplane={status}; unresolved)"
        )
    gap = _incomplete_dig_gap_kind(dx)
    if gap == "query_failure":
        return (
            f"**Result:** {RESULT_INCOMPLETE_QUERY} "
            f"(dataplane={status}; unresolved)"
        )
    if gap == "needs_traffic":
        return (
            f"**Result:** {RESULT_INCOMPLETE_NEEDS_TRAFFIC} "
            f"(dataplane={status}; unresolved)"
        )
    return (
        f"**Result:** {RESULT_INCOMPLETE_GENERIC} "
        f"(dataplane={status}; unresolved)"
    )


def _incomplete_dig_next_action(dx: dict[str, Any]) -> str:
    structured = dx.get("verification_gap")
    if isinstance(structured, dict) and structured.get("next_check"):
        return scrub_internal_ids(str(structured["next_check"]))
    kind = _incomplete_dig_stop_kind(dx)
    if kind == "timeout":
        return (
            "Address the dataplane LLM timeout first (retry the dig, raise the "
            "Fabric chat timeout, or shorten the dig), then re-verify — do not "
            "raise --max-dataplane-tools for a timeout alone."
        )
    if kind == "budget":
        return (
            "Re-run verification with a higher dataplane tool budget "
            "(--max-dataplane-tools), or investigate manually."
        )
    if kind == "no_conclusion":
        return (
            "Retry the dataplane dig (model returned neither tools nor "
            "conclude_dataplane) — not a Fabric timeout; do not raise "
            "--max-dataplane-tools for this alone."
        )
    gap = _incomplete_dig_gap_kind(dx)
    if gap == "query_failure":
        return NEXT_CORRECTED_QUERY
    if gap == "needs_traffic":
        return NEXT_CUSTOMER_TRAFFIC
    return "Re-run dataplane verification, or investigate manually."


def _incomplete_dig_followup(name: str, dx: dict[str, Any]) -> str:
    structured = dx.get("verification_gap")
    if isinstance(structured, dict) and structured.get("next_check"):
        return scrub_internal_ids(str(structured["next_check"]))
    if isinstance(dx.get("verification_gap"), dict):
        return f"Finish verification for `{name}`: {_incomplete_dig_next_action(dx)}"
    kind = _incomplete_dig_stop_kind(dx)
    if kind == "timeout":
        return (
            f"Re-run dataplane dig for `{name}` after fixing the Fabric chat "
            "timeout so PE-side verification can finish (not a tool-budget "
            "issue)."
        )
    if kind == "budget":
        return (
            f"Re-run dataplane dig for `{name}` with a higher "
            "--max-dataplane-tools budget so the dig can conclude."
        )
    if kind == "no_conclusion":
        return (
            f"Re-run dataplane dig for `{name}` so the model calls "
            "conclude_dataplane (prior dig stopped without a conclusion; "
            "not a timeout or tool-budget issue)."
        )
    gap = _incomplete_dig_gap_kind(dx)
    if gap == "query_failure":
        return (
            f"Correct the BG:BD / forwarding query for `{name}`, then finish "
            "PE-side MAC/install checks (not another full dig or BGP summary)."
        )
    if gap == "needs_traffic":
        return (
            f"Obtain known customer endpoints and access for a scoped L2 test "
            f"on `{name}` (passive MAC may never appear; PE-loopback ≠ "
            "customer L2 path)."
        )
    return (
        f"Re-run dataplane dig for `{name}` to finish PE-side verification."
    )


def format_services_operator(
    case: CaseFile, *, services_detail: bool = False
) -> list[str]:
    lines: list[str] = []
    if not services_detail:
        lines.extend(format_incomplete_checks_grouped(case))

    rows = _services_to_render(case, services_detail=services_detail)
    total_services = len(_iter_service_records(case))
    shown = len(rows)
    omitted = max(0, total_services - shown) if not services_detail else 0

    if not rows and not lines:
        return ["(none)"]

    if not services_detail and omitted:
        shown_names = {str(name) for name, _rec in rows}
        up_omitted = sum(
            1
            for name, rec in _iter_service_records(case)
            if name not in shown_names
            and str(rec.get("status") or "").lower() in {"up", "ok"}
        )
        grouped_unknown = sum(
            c for _e, _r, c in _incomplete_checks_by_endpoint_reason(case)
        )
        omit_bits: list[str] = []
        if up_omitted:
            omit_bits.append(f"{up_omitted} SystemUp")
        if grouped_unknown:
            omit_bits.append(f"{grouped_unknown} unknown (grouped above)")
        # Residual omitted not explained by SystemUp or grouped unknowns.
        other = omitted - up_omitted - grouped_unknown
        if other > 0:
            omit_bits.append(f"{other} other")
        detail = ", ".join(omit_bits) if omit_bits else f"{omitted} instances"
        lines.append(
            f"*Only these {shown} investigated/impaired instance"
            f"{'s' if shown != 1 else ''} received additional dig coverage; "
            f"{detail} omitted (baseline sync only — not fleet-wide "
            "dataplane verification). "
            "Use `--services-detail` for per-instance sections.*"
        )
        lines.append("")

    if services_detail:
        # Full dump still benefits from the grouped summary first.
        grouped = format_incomplete_checks_grouped(case)
        if grouped:
            lines.extend(grouped)

    if not rows:
        if not lines:
            return ["(none)"]
        return lines

    for name, rec in rows:
        stype = str(rec.get("service_type") or "").upper() or "SERVICE"
        title = f"{stype} · {name}"
        lines.append(f"### {title}")
        lines.append("")
        endpoints = _endpoint_summary(rec)
        if endpoints:
            lines.append(f"**Endpoints:** {endpoints}")

        dx = rec.get("_diagnosis") if isinstance(rec.get("_diagnosis"), dict) else None
        sys_s = str(rec.get("system_status") or rec.get("status") or "").lower()
        dp_s = str(rec.get("dataplane_status") or "").lower()

        if dx:
            complete = dx.get("complete", True)
            status = str(dx.get("dataplane_status") or "?").lower()
            if complete is False:
                lines.append(_incomplete_result_line(dx))
            elif status in {"up", "ok"}:
                lines.append(f"**Result:** {RESULT_PE_SIDE_PASSED}")
            elif status in {"down", "degraded"}:
                lines.append(
                    f"**Result:** Confirmed fault — dataplane {status}"
                )
            else:
                lines.append(f"**Result:** Dataplane={status}")
            cov = case.service_coverage.get(name)
            if cov:
                lines.append(f"**Coverage:** {_coverage_label(cov)}")
            obs = scrub_internal_ids(str(dx.get("observed") or "").strip())
            cause = scrub_internal_ids(str(dx.get("cause") or "").strip())
            # Essential evidence beside the finding; collector/device detail later.
            if obs:
                lines.append("")
                lines.extend(_format_evidence_block(obs))
            if cause:
                if not obs:
                    lines.append("")
                lines.append(f"**Cause:** {cause}")
            structured_gap = dx.get("verification_gap")
            if isinstance(structured_gap, dict):
                lines.append("")
                lines.append("**Verification gap:**")
                for key, label in (("missing_check", "Missing check"),
                                   ("blocker", "Blocker"), ("direction", "Direction"),
                                   ("required_access", "Required access")):
                    value = structured_gap.get(key)
                    if value:
                        lines.append(f"- {label}: {scrub_internal_ids(str(value))}")
            uncertainty: list[str] = []
            live = rec.get("live_l2") if isinstance(rec.get("live_l2"), dict) else {}
            for ep in live.get("endpoints") or []:
                if isinstance(ep, dict) and ep.get("error") == "ac_not_found":
                    uncertainty.append(
                        "Initial probe returned `ac_not_found`; contradicted "
                        "by service-specific checks."
                    )
                    break
            if complete is False:
                uncertainty.append(
                    "Verification did not finish; do not treat the service as explained."
                )
                gap = _incomplete_dig_gap_kind(dx)
                stop = _incomplete_dig_stop_kind(dx)
                if not structured_gap and stop == "other" and gap == "needs_traffic":
                    uncertainty.append(
                        "Passive PE checks may never see MACs without "
                        "customer-generated traffic."
                    )
                elif stop == "other" and gap == "query_failure":
                    uncertainty.append(
                        "A failed or wrong-object query is not proof of a "
                        "forwarding fault."
                    )
            if status in {"up", "ok"}:
                uncertainty.append(UNCERTAINTY_DELIVERY_UNVERIFIED)
            if uncertainty:
                lines.append("")
                lines.extend(_format_uncertainty_block(uncertainty))
            body = _live_l2_body(rec)
            if body:
                lines.append("")
                lines.append("**Collector detail:**")
                for line in body:
                    lines.append(line)
            fix = scrub_internal_ids(str(dx.get("fix_suggestion") or "").strip())
            lines.append("")
            if complete is False and isinstance(structured_gap, dict):
                next_check = structured_gap.get("next_check") or "Review the missing evidence."
                lines.append(f"**Next check:** {scrub_internal_ids(str(next_check))}")
                if structured_gap.get("blocker") == "endpoint_access_unavailable":
                    lines.append("Operator follow-up: customer-host access and traffic testing are outside this agent.")
            elif complete is not False and fix:
                next_line = f"**Next action:** {fix}"
                if status in {"down", "degraded"}:
                    next_line += " (human must approve any config change)."
                lines.append(next_line)
            elif complete is False:
                lines.append(
                    f"**Next action:** {_incomplete_dig_next_action(dx)}"
                )
            elif status in {"up", "ok"}:
                lines.append(f"**Next action:** {NEXT_SCOPED_REACHABILITY}")
            else:
                lines.append(
                    "**Next action:** Human review required before any remediation."
                )
        else:
            if sys_s == "up" or dp_s == "up" or str(rec.get("status") or "").lower() == "up":
                lines.append("**Result:** Reported up by collection")
            else:
                status = rec.get("status") or sys_s or dp_s or "unknown"
                reason = _collection_status_reason(rec)
                verification = _collection_verification_problem(rec)
                if verification and reason:
                    lines.append(
                        f"**Result:** Collection verification incomplete "
                        f"({reason}) — not a confirmed forwarding fault"
                    )
                elif reason:
                    lines.append(
                        f"**Result:** Reported {status} by collection ({reason})"
                    )
                else:
                    lines.append(f"**Result:** Reported {status} by collection")
            cov = _service_coverage_code(case, name, rec)
            lines.append(f"**Coverage:** {_coverage_label(cov)}")
            lines.append("")
            body = _live_l2_body(rec)
            for line in body:
                lines.append(line)
            if body:
                lines.append("")
            if cov == "budget_skipped":
                lines.append(
                    "**Next action:** Raise investigation budget "
                    "(--max-drill-issues / --max-dataplane-tools) to verify this instance."
                )
            elif cov == "collection_concluded":
                if _collection_verification_problem(rec):
                    lines.append(
                        "**Next action:** Retry live sync/MCP for the affected "
                        "endpoint(s); collection could not finish verification — "
                        "do not treat as a confirmed dataplane outage without "
                        "further evidence. Use `--service-id` for an LLM dig if "
                        "needed."
                    )
                else:
                    lines.append(
                        "**Next action:** Investigate the collection findings "
                        "(live L2 / sync). Use `--service-id` for an LLM dig if "
                        "you want a re-verify — raising dig budget alone will not "
                        "select collection-down instances."
                    )
            elif cov == "category_peer_skipped":
                stype = str(rec.get("service_type") or "").strip() or "this type"
                lines.append(
                    f"**Next action:** Another `{stype}` instance is the "
                    "typed-category dataplane LLM sample; this peer was not "
                    "re-verified."
                )
            else:
                lines.append(
                    "**Next action:** LLM investigation was not performed; "
                    "dataplane readiness remains unverified. Use --service-id "
                    "with LLM enabled for a focused investigation."
                )

        lines.append("")
        lines.append(f"*Service ID: `{name}`*")
        lines.append("")
    return lines


def _site_label(device: str) -> str:
    """wash-data-sw → WASH for operator follow-up phrasing."""
    d = str(device or "").strip()
    lower = d.lower()
    for suf in ("-data-sw", "-sw", "-router", "-pe"):
        if lower.endswith(suf):
            d = d[: -len(suf)]
            break
    return d.upper() if d else str(device or "").strip()


def format_followup_operator(case: CaseFile) -> list[str]:
    """Ordered operator actions: service faults → collection → gaps → inventory.

    Each item names a target and what resolving it achieves. Avoid generic
    "retry everything", inconclusive service-sync loops, or config changes
    unsupported by evidence.
    """
    items: list[str] = []
    fault_names: set[str] = set()
    dp_findings = _dataplane_by_name(case)
    for name, dx in dp_findings.items():
        status = str(dx.get("dataplane_status") or "").lower()
        if dx.get("complete") is not False and status in {"down", "degraded"}:
            fault_names.add(name)
            items.append(
                f"Prioritize dataplane {status} on `{name}`: review the "
                "service evidence and supported next steps before remediation "
                "(human must approve any configuration/state change)."
            )
    for issue in case.issues:
        name = str(issue.get("edge_id") or "").strip()
        if (name and name not in fault_names and name not in dp_findings
                and issue.get("code") in {"service_down", "service_degraded"}
                and issue.get("status") in {None, "open", "needs_human", "escalated"}):
            fault_names.add(name)
            items.append(f"Investigate collection-reported fault on `{name}` "
                         "before remediation (see Services for evidence).")
    for row in case.last_known_service_faults:
        name = row["service"]
        if name not in fault_names and not row.get("rechecked"):
            fault_names.add(name)
            items.append(
                f"Recheck `{name}` first: last known {row['status']} "
                f"(run `{row.get('last_observed_run_id', 'unknown')}`); "
                f"{row.get('verification', 'not rechecked')}. "
                "Do not treat a baseline pass or sampling omission as recovery."
            )

    # 1) One action per device for live-MCP quarantine and/or sync unknowns.
    from nso_facts.mcp_client import (
        live_mcp_failure_kind,
        parse_live_mcp_collection_phase,
        parse_live_mcp_read_timeout_sec,
    )

    quarantined_by_device: dict[str, str] = {}
    for issue in case.issues:
        if str(issue.get("code") or "") != "device_live_unreachable":
            continue
        devices = issue.get("devices") or []
        label = str(devices[0]).strip() if devices else ""
        if not label or label in quarantined_by_device:
            continue
        quarantined_by_device[label] = live_mcp_failure_kind(
            str(issue.get("message") or "")
        )

    involvement = _verification_unknown_involvement_by_device(case)
    grouped_names = _verification_unknown_service_names(case)

    attributed_devices = sorted(
        involvement.keys(), key=lambda d: (-involvement[d], d)
    )
    quarantine_messages = {
        str((i.get("devices") or [None])[0]).strip(): str(i.get("message") or "")
        for i in case.issues
        if str(i.get("code") or "") == "device_live_unreachable"
        and (i.get("devices") or [None])[0]
    }

    def _timeout_collection_followup(device: str, *, services_n: int | None) -> str:
        msg = quarantine_messages.get(device, "")
        timeout = parse_live_mcp_read_timeout_sec(msg)
        phase = parse_live_mcp_collection_phase(msg)
        site = _site_label(device)
        if services_n is None:
            resolve = (
                f"to restore automated live checks for `{device}`"
            )
        else:
            resolve = (
                f"to restore automated live checks for `{device}` and clear "
                f"collection gaps for up to {services_n} unknown "
                f"service{'s' if services_n != 1 else ''} attributed here"
            )
        if timeout:
            what = (
                f"the repeated {timeout}-second NSO API timeout during "
                f"{site} {phase}"
            )
        else:
            # NEDCOM connect timeouts often lack read timeout=Ns — do not
            # invent a placeholder like "N-second".
            what = (
                f"the NSO live-MCP connect/timeout failure during "
                f"{site} {phase}"
            )
        return (
            f"Investigate {what}; compare the same operation through MCP "
            f"and the NSO CLI {resolve} (manual NSO access may still succeed "
            "— not device down)."
        )

    for device in attributed_devices:
        n = involvement[device]
        kind = quarantined_by_device.get(device) or live_mcp_failure_kind(
            quarantine_messages.get(device, "")
        )
        if kind == "timeout":
            items.append(_timeout_collection_followup(device, services_n=n))
        elif kind == "unreachable":
            items.append(
                f"Restore live reachability to `{device}` to resume automated "
                f"checks and clear up to {n} unknown "
                f"service{'s' if n != 1 else ''} attributed here "
                "(strong unreachability evidence this run)."
            )
        else:
            items.append(
                f"Restore live collection evidence for `{device}` to clear up "
                f"to {n} unknown service{'s' if n != 1 else ''} attributed "
                "here (sync/query incomplete — not confirmed outages; avoid "
                "inconclusive service-sync loops)."
            )

    quarantine_only = sorted(
        d for d in quarantined_by_device if d not in involvement
    )
    for device in quarantine_only:
        kind = quarantined_by_device[device]
        if kind == "unreachable":
            items.append(
                f"Restore live reachability to `{device}` to resume automated "
                "live MCP (no unknown services attributed in this grouping)."
            )
        elif kind == "timeout":
            items.append(_timeout_collection_followup(device, services_n=None))
        else:
            items.append(
                f"Investigate failed live MCP for `{device}` to restore "
                "automated collection (no unknown services attributed in "
                "this grouping)."
            )

    # Do not add generic "retry service sync" / "reassess all unknowns" —
    # per-device actions above already state what they resolve.

    # Other affected services (collection unhealthy, dig incomplete/faults)
    # not already covered by the endpoint-unknown groups above.
    affected: list[str] = []
    seen_svc: set[str] = set(grouped_names) | fault_names

    def _add_affected(name: str, detail: str) -> None:
        key = name.strip()
        if not key or key in seen_svc:
            return
        seen_svc.add(key)
        affected.append(detail)

    services = services_from_case(case)
    for name, cov in (case.service_coverage or {}).items():
        if cov != "collection_concluded":
            continue
        if name in grouped_names:
            continue
        rec = None
        for _k, cand in services.items():
            if isinstance(cand, dict) and cand.get("name") == name:
                rec = cand
                break
        if rec and _collection_verification_problem(rec):
            _add_affected(
                name,
                f"Finish live collection for `{name}` to replace unknown "
                "status with evidence (sync/query incomplete — not a "
                "confirmed outage; avoid inconclusive service-sync loops).",
            )
        else:
            _add_affected(
                name,
                f"Investigate `{name}` using the collection finding already "
                "reported unhealthy (re-verify before any config change).",
            )

    for issue in case.issues:
        if issue.get("status") not in {None, "open"}:
            continue
        code = str(issue.get("code") or "")
        if code not in {"service_down", "service_degraded"}:
            continue
        name = str(issue.get("edge_id") or "").strip()
        if not name:
            continue
        status = "down" if code == "service_down" else "degraded"
        _add_affected(
            name,
            f"Investigate `{name}` to confirm or clear collection-reported "
            f"{status} before remediation.",
        )

    for name, dx in _dataplane_by_name(case).items():
        if dx.get("complete") is False:
            _add_affected(name, _incomplete_dig_followup(name, dx))
            continue
        status = str(dx.get("dataplane_status") or "").lower()
        if status in {"down", "degraded"}:
            _add_affected(
                name,
                f"Remediate dataplane {status} on `{name}` only with evidence-"
                "backed changes (human must approve any config change).",
            )
            continue
        # Residual live_l2 ac_not_found on an already-complete dig is collector
        # triage, not unfinished verification — do not fold into the
        # "Finish verification for N …" count (that must match incomplete digs).
        if status in {"up", "ok"}:
            continue
        live_issue = _service_issue_for(case, name)
        live = None
        if live_issue and isinstance(live_issue.get("live_l2"), dict):
            live = live_issue["live_l2"]
        rec = None
        for _k, cand in services.items():
            if isinstance(cand, dict) and cand.get("name") == name:
                rec = cand
                break
        if rec and isinstance(rec.get("live_l2"), dict):
            live = rec["live_l2"]
        if live:
            for ep in live.get("endpoints") or []:
                if isinstance(ep, dict) and ep.get("error") == "ac_not_found":
                    _add_affected(
                        name,
                        f"Reassess `{name}`: initial `ac_not_found` "
                        "contradicted by service-specific checks "
                        "(resolve collector vs dig disagreement).",
                    )
                    break

    incomplete_n = sum(
        1
        for _n, dx in _dataplane_by_name(case).items()
        if dx.get("complete") is False and _n not in fault_names and _n not in grouped_names
    )
    if len(affected) <= 3:
        items.extend(affected)
    elif affected:
        # Prefer the incomplete-dig count when that is what operators compare
        # against Result/Coverage; mention extras only if present.
        extra_n = len(affected) - incomplete_n
        if incomplete_n and extra_n <= 0:
            items.append(
                f"Finish verification for {incomplete_n} service"
                f"{'s' if incomplete_n != 1 else ''} with incomplete "
                "dataplane digs (see Services for targets)."
            )
        elif incomplete_n and extra_n > 0:
            items.append(
                f"Finish verification for {incomplete_n} incomplete "
                f"dataplane dig{'s' if incomplete_n != 1 else ''}, plus "
                f"{extra_n} other service"
                f"{'s' if extra_n != 1 else ''} with collection/dig "
                "findings (see Services for targets)."
            )
        else:
            items.append(
                f"Finish verification for {len(affected)} additional services "
                "with collection or dig findings (see Services for targets)."
            )

    # 3) Neighbor-mapping inventory (collapsed; after reachability/services)
    mapping = [
        i
        for i in case.issues
        if str(i.get("code") or "")
        in {"unknown_neighbor_address", "unknown_neighbor_system_id"}
    ]
    if mapping:
        items.append(
            f"Classify {len(mapping)} neighbor-mapping observation"
            f"{'s' if len(mapping) != 1 else ''} so inventory/peer labels "
            "stop appearing as open routing gaps."
        )

    # 4) Real interface inventory/mapping only — never drop-counter Review alone
    inv_devices = [
        str(row.get("device"))
        for row in _device_rows(case)
        if row.get("device") and _device_needs_inventory_followup(row)
    ]
    if len(inv_devices) == 1:
        items.append(
            f"Classify inventory/mapping observations on `{inv_devices[0]}` "
            "to separate onboarding gaps from faults (Review is triage only)."
        )
    elif len(inv_devices) > 1:
        items.append(
            f"Classify inventory/mapping observations on {len(inv_devices)} "
            "devices to separate onboarding gaps from faults (triage only; "
            "historical drop counters alone are not urgent)."
        )

    # Intentionally not-selected / not-dug services are covered by the
    # Services table coverage statement — do not list them as follow-up tasks.

    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    if not out:
        return ["(none)"]
    return [f"{i}. {item}" for i, item in enumerate(out, start=1)]


def format_result_line(case: CaseFile) -> str:
    """One-line Result for the report header (deterministic)."""
    dp = list(_dataplane_by_name(case).values())
    mapping_n = sum(
        1
        for i in case.issues
        if str(i.get("code") or "")
        in {"unknown_neighbor_address", "unknown_neighbor_system_id"}
    )
    quarantined_issues = [
        i
        for i in case.issues
        if str(i.get("code") or "") == "device_live_unreachable"
    ]
    quarantined_n = len(quarantined_issues)
    incomplete = [d for d in dp if d.get("complete") is False]
    faults = [
        d
        for d in dp
        if d.get("complete") is not False
        and str(d.get("dataplane_status") or "").lower() in {"down", "degraded"}
    ]
    ups = [
        d
        for d in dp
        if d.get("complete") is not False
        and str(d.get("dataplane_status") or "").lower() in {"up", "ok"}
    ]
    parts: list[str] = []
    if quarantined_n:
        from nso_facts.mcp_client import (
            live_mcp_failure_kind,
            parse_live_mcp_failed_operation,
            parse_live_mcp_read_timeout_sec,
        )

        kinds = {
            live_mcp_failure_kind(str(i.get("message") or ""))
            for i in quarantined_issues
        }
        involvement = _verification_unknown_involvement_by_device(case)
        if kinds == {"timeout"}:
            ops = {
                parse_live_mcp_failed_operation(str(i.get("message") or ""))
                for i in quarantined_issues
            }
            timeouts = {
                parse_live_mcp_read_timeout_sec(str(i.get("message") or ""))
                for i in quarantined_issues
            }
            timeouts.discard(None)
            op = next(iter(ops)) if len(ops) == 1 else "NSO live-MCP collection"
            timeout_bit = ""
            if len(timeouts) == 1:
                timeout_bit = f", read timeout={next(iter(timeouts))}s"
            elif timeouts:
                timeout_bit = (
                    ", read timeout="
                    + "/".join(sorted(timeouts, key=lambda x: float(x or 0)))
                    + "s"
                )
            device_bits: list[str] = []
            for issue in sorted(
                quarantined_issues,
                key=lambda i: str((i.get("devices") or [""])[0]),
            ):
                dev = str((issue.get("devices") or [""])[0]).strip()
                if not dev:
                    continue
                n = involvement.get(dev, 0)
                device_bits.append(
                    f"`{dev}` ({n} unknown service{'s' if n != 1 else ''})"
                )
            device_list = ", ".join(device_bits) if device_bits else (
                f"{quarantined_n} device{'s' if quarantined_n != 1 else ''}"
            )
            parts.append(
                f"{quarantined_n} device"
                f"{'s' if quarantined_n != 1 else ''} hit automated "
                f"{op} timeout{timeout_bit}: {device_list} "
                "(further live MCP skipped; manual NSO access may still "
                "succeed — not proof devices are down)."
            )
        elif kinds == {"unreachable"}:
            parts.append(
                f"{quarantined_n} device"
                f"{'s' if quarantined_n != 1 else ''} live-unreachable "
                "(further live MCP skipped)."
            )
        else:
            parts.append(
                f"{quarantined_n} device"
                f"{'s' if quarantined_n != 1 else ''} live-query-failed "
                "(further live MCP skipped)."
            )
    unknown_groups = _verification_unknown_by_endpoints(case)
    unknown_summary = _format_verification_unknown_summary(unknown_groups)
    if unknown_summary:
        parts.append(unknown_summary + ".")
    else:
        status_counts = _service_status_counts(case)
        unknown_n = status_counts.get("unknown", 0)
        if unknown_n:
            parts.append(
                f"{unknown_n} service{'s' if unknown_n != 1 else ''} unknown "
                "(insufficient evidence; not confirmed down)."
            )
    if faults:
        parts.append(
            f"{len(faults)} investigated service"
            f"{'s' if len(faults) != 1 else ''} with dataplane faults."
        )
    elif ups and not incomplete and not unknown_groups:
        parts.append(
            f"No fault identified in the {len(ups)} investigated service"
            f"{'s' if len(ups) != 1 else ''}."
        )
    elif incomplete:
        parts.append(
            f"{len(incomplete)} service verification"
            f"{'s' if len(incomplete) != 1 else ''} incomplete."
        )
    elif not dp and not quarantined_n and not unknown_groups:
        open_n = sum(1 for i in case.issues if i.get("status") == "open")
        if open_n:
            parts.append(
                f"{open_n} open issue{'s' if open_n != 1 else ''} "
                f"{'remain' if open_n != 1 else 'remains'}."
            )
        else:
            parts.append("No dataplane investigations recorded.")
    if mapping_n:
        parts.append(
            f"{mapping_n} inventory mapping observation"
            f"{'s' if mapping_n != 1 else ''} need clarification."
        )
    health_flags = [r for r in _device_rows(case) if _device_has_health_review(r)]
    if health_flags:
        parts.append(
            "Device inventory/counter observations need classification "
            "(Review is triage, not a confirmed fault)."
        )
    return " ".join(parts) if parts else "Run completed."


def service_sync_note_from_case(case: CaseFile) -> str | None:
    for ev in case.evidence:
        if ev.get("kind") != "spine" or ev.get("role") != "service":
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        note = extra.get("service_sync_note") if isinstance(extra, dict) else None
        if isinstance(note, str) and note.strip():
            return note.strip()
    return None


def format_run_details(
    case: CaseFile,
    *,
    dry_run: bool = False,
    reporting_notes: list[str] | None = None,
) -> list[str]:
    from diagnostic_mas.dataplane_verify import dataplane_tools_cap

    b = case.budget
    dp_cap = dataplane_tools_cap(b)
    dp_used = int(getattr(b, "dataplane_tools_used", 0) or 0)
    digs_n = sum(1 for d in case.diagnoses if d.get("kind") == "dataplane")
    if digs_n <= 0:
        # Fall back to incomplete/finding evidence if diagnoses absent.
        digs_n = sum(
            1
            for e in case.evidence
            if e.get("kind") in {"dataplane_finding", "dataplane_incomplete"}
        )
    if digs_n > 0:
        dp_tools_line = (
            f"**Dataplane dig tools:** {dp_used} used across {digs_n} dig"
            f"{'s' if digs_n != 1 else ''} · {dp_cap} allowed per dig "
            "(per-dig ceiling, not a fleet total)"
        )
    else:
        dp_tools_line = (
            f"**Dataplane dig tools:** {dp_used} used · "
            f"{dp_cap} allowed per dig"
        )
    lines = [
        f"**Evidence records:** {len(case.evidence)}",
        f"**Diagnoses:** {len(case.diagnoses)}",
        dp_tools_line,
        f"**Drill investigations:** {getattr(b, 'drill_issues_used', 0)} / "
        f"{getattr(b, 'max_drill_issues', 0)} "
        f"(tools used {getattr(b, 'drills_used', 0)})",
        f"**Deep checks / handoffs:** "
        f"{b.deep_checks_used}/{b.max_deep_checks} · "
        f"{b.handoffs_used}/{b.max_handoffs}",
    ]
    timed = [(name, dx.get("investigation")) for name, dx in _dataplane_by_name(case).items()
             if isinstance(dx.get("investigation"), dict)]
    if timed:
        lines.extend(["", "**Per-service investigations:**"])
        for name, m in timed:
            lines.append(
                f"- `{name}`: {m['duration_seconds']:.1f}s total "
                f"(LLM {m['llm_seconds']:.1f}s; MCP {m['mcp_seconds']:.1f}s; "
                f"other {m['other_seconds']:.1f}s); tools {m['tools_used']}/{m['tools_allowed']}; "
                f"rounds {m['rounds_used']}/{m['rounds_allowed']}; "
                f"LLM requests {m['llm_requests']}; stopped: {m['stop_reason']}; "
                f"tools remaining: {m['tools_remaining']}."
            )
    for note in reporting_notes or []:
        if note.strip():
            lines.append(f"**Reporting note:** {scrub_internal_ids(note.strip())}")
    if dry_run:
        lines.append(
            "**Retention:** Dry run; case evidence was not saved."
        )
    # Optional leftover hypotheses (not ground truth)
    drilled_ids: set[str] = set()
    for ev in case.evidence:
        if ev.get("kind") != "drill_finding":
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        for field in ("issue_id", "issue_edge_id"):
            val = payload.get(field)
            if val is not None and str(val).strip():
                drilled_ids.add(str(val).strip())
    hy_lines: list[str] = []
    seen: set[str] = set()
    for hy in case.hypotheses:
        ids = {str(x).strip() for x in (hy.get("issue_ids") or []) if x}
        if ids & drilled_ids:
            continue
        text = scrub_internal_ids(str(hy.get("text") or "").strip())
        if not text or text in seen:
            continue
        seen.add(text)
        hy_lines.append(text)
    if hy_lines:
        lines.append("")
        lines.append("**Open hypotheses (not ground truth):**")
        for text in hy_lines:
            lines.append(f"- {text}")
    return lines


def scope_counts(case: CaseFile) -> tuple[int, int]:
    devices = set(case.focus_devices or []) or set(case.device_names or [])
    if not devices:
        devices = {str(r.get("device")) for r in _device_rows(case) if r.get("device")}
    services = services_from_case(case)
    svc_n = len(services) if services else len(_dataplane_by_name(case))
    if not svc_n:
        svc_n = sum(1 for i in case.issues if i.get("layer") == "services")
    return len(devices), svc_n


def format_appendix_full(case: CaseFile) -> list[str]:
    lines = ["## Appendix: Detailed Device Analysis", ""]
    lines.extend(format_detailed_devices_section(case))
    return lines
