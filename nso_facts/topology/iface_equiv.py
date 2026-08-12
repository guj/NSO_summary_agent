"""Admin interface equivalences + heuristic NSO↔box candidates."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable

from nso_facts.topology.interfaces import _IOS_INTERFACE_PREFIXES

logger = logging.getLogger(__name__)

# device -> nso name -> box name(s)
EquivalenceMap = dict[str, dict[str, list[str]]]

# Repo-relative draft of heuristic suggestions (review before confirming).
SUGGESTED_EQUIVALENCES_DRAFT_REL = "config/to_confirm.interface-equivalence.json"

_KIND_NOTES = {
    "type_change": (
        "suggested: same port address, different type — confirm or delete"
    ),
    "breakout": "suggested: breakout children — confirm list or delete",
    "missing": (
        "suggested: no live candidate (phantom?) — delete NSO config "
        "or leave empty box after review"
    ),
}


def interface_port_address(name: str) -> str | None:
    """Return port address after IOS-XR type prefix (e.g. ``0/0/0/32``)."""
    if not name:
        return None
    for long_prefix, short_prefix in _IOS_INTERFACE_PREFIXES:
        if name.startswith(long_prefix):
            rem = name[len(long_prefix) :]
            return rem or None
        if name.startswith(short_prefix):
            rem = name[len(short_prefix) :]
            if rem:
                return rem
    # Unknown type: strip leading letters
    match = re.match(r"^[A-Za-z]+(.+)$", name)
    if match:
        return match.group(1) or None
    return None


def find_live_candidates(
    static_iface: str,
    live_names: Iterable[str],
) -> tuple[list[str], str]:
    """Heuristic live counterparts for an unmatched static interface.

    Returns ``(candidates_sorted, kind)`` where kind is
    ``type_change`` | ``breakout`` | ``missing``.
    """
    addr = interface_port_address(static_iface)
    if not addr:
        return [], "missing"

    candidates: list[str] = []
    for live in live_names:
        live_addr = interface_port_address(live)
        if live_addr is None:
            continue
        if live_addr == addr or live_addr.startswith(addr + "/"):
            candidates.append(live)

    candidates = sorted(set(candidates))
    if not candidates:
        return [], "missing"

    has_child = any(
        (interface_port_address(c) or "").startswith(addr + "/") for c in candidates
    )
    if has_child or len(candidates) > 1:
        return candidates, "breakout"
    return candidates, "type_change"


def compact_box_list(names: list[str]) -> str:
    """Format box names; compact contiguous breakout indexes when possible."""
    if not names:
        return "(not on box)"
    if len(names) == 1:
        return names[0]

    # Group by prefix before final /index
    parts: list[tuple[str, int]] = []
    for name in names:
        if "/" not in name:
            return ", ".join(names)
        head, _, tail = name.rpartition("/")
        if not tail.isdigit():
            return ", ".join(names)
        parts.append((head, int(tail)))

    heads = {h for h, _ in parts}
    if len(heads) != 1:
        return ", ".join(names)

    head = parts[0][0]
    indexes = sorted(i for _, i in parts)
    expected = list(range(indexes[0], indexes[0] + len(indexes)))
    if indexes != expected:
        return ", ".join(names)
    return f"{head}/{indexes[0]}–{indexes[-1]}"


def load_interface_equivalences(path: Path | None) -> EquivalenceMap:
    """Load admin equivalences; empty map if unset/missing/invalid."""
    if path is None:
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("INTERFACE_EQUIVALENCES_FILE unreadable (%s): %s", path, exc)
        return {}

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("INTERFACE_EQUIVALENCES_FILE invalid JSON (%s): %s", path, exc)
        return {}

    rows = data.get("equivalences") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        logger.warning(
            "INTERFACE_EQUIVALENCES_FILE missing equivalences list (%s)", path
        )
        return {}

    out: EquivalenceMap = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        device = row.get("device")
        nso = row.get("nso")
        box = row.get("box")
        if not isinstance(device, str) or not isinstance(nso, str):
            continue
        box_list = _normalize_box(box)
        if not box_list:
            continue
        out.setdefault(device, {})[nso] = box_list
    return out


def _normalize_box(box: Any) -> list[str]:
    if isinstance(box, str) and box.strip():
        return [box.strip()]
    if isinstance(box, list):
        names = [str(x).strip() for x in box if str(x).strip()]
        return names
    return []


def resolve_equivalence(
    equivalences: EquivalenceMap,
    device: str,
    nso: str,
) -> list[str] | None:
    """Return admin box list for device+nso, or None."""
    by_nso = equivalences.get(device) or {}
    box = by_nso.get(nso)
    return list(box) if box else None


def build_suggested_equivalences_draft(
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the to-confirm JSON payload from ``config_live_mismatch`` issues."""
    rows: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if issue.get("code") != "config_live_mismatch":
            continue
        if issue.get("confirmed"):
            continue
        device = issue.get("device")
        nso = issue.get("nso")
        if not isinstance(device, str) or not isinstance(nso, str):
            continue
        candidates = issue.get("candidates") or []
        if not isinstance(candidates, list):
            candidates = []
        cand_names = [str(c) for c in candidates if str(c).strip()]
        if not cand_names:
            box: Any = []
        elif len(cand_names) == 1:
            box = cand_names[0]
        else:
            box = cand_names
        kind = str(issue.get("kind") or "missing")
        rows.append(
            {
                "device": device,
                "nso": nso,
                "box": box,
                "note": _KIND_NOTES.get(kind, f"suggested kind={kind}"),
            }
        )
    rows.sort(key=lambda r: (r["device"], r["nso"]))
    return {
        "_comment": (
            "DRAFT suggested matches from the latest agent run. Review each row, "
            "copy keepers to config/interface-equivalences.json, then set "
            "INTERFACE_EQUIVALENCES_FILE. Rows with box=[] are phantoms — fix or "
            "remove NSO config instead of confirming."
        ),
        "equivalences": rows,
    }


def write_suggested_equivalences_draft(
    path: Path,
    issues: list[dict[str, Any]],
) -> int:
    """Write draft file; return number of suggested rows."""
    payload = build_suggested_equivalences_draft(issues)
    rows = payload.get("equivalences") or []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return len(rows)


def suggested_equivalences_report_note(
    count: int,
    draft_rel: str | None = None,
) -> str:
    """Bold action line for the top of the Devices section."""
    rel = draft_rel or SUGGESTED_EQUIVALENCES_DRAFT_REL
    noun = "match" if count == 1 else "matches"
    return (
        f"**Action required:** {count} unconfirmed NSO↔box interface {noun} "
        f"— review {rel}"
    )
