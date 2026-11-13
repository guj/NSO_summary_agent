"""The IS-IS overload bit each router advertises, from one read of the link-state database per scan.

``show isis database level 2`` on one router lists every router's LSP with its ``ATT/P/OL`` bits, so a
single read stands for the fleet. The first router with an IS-IS adjacency is asked; when it gives no
table the next is asked, up to three. A router that sets the bit is a lead, never a status or an issue:
the bit is often set on purpose during maintenance, and the agent cannot tell that from a router left
in overload. A table that was not read is "not read", never "none set".
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from diagnostic_mas.case import CaseFile, add_evidence
from diagnostic_mas.link_optics import _ends
from nso_facts.mcp_client import call_mcp

KIND = "isis_overload"
TRIES = 3
COMMAND = "isis database level 2"
# One LSP row: ``name.00-00``, the reader's own row marked ``*`` with a hold time of ``/*``, then ATT/P/OL.
_LSP = re.compile(r"^(\S+?)\.(\d\d)-(\d\d)\s+\*?\s*0x\w+\s+0x\w+\s+\d+\s*/\s*(?:\d+|\*)\s+(\d)/(\d)/(\d)\s*$", re.M)
_TOTAL = re.compile(r"Total Level-2 LSP count:\s*(\d+)")


def parse_isis_database(text: str) -> dict[str, Any] | None:
    """Each router's overload bit from its first fragment, and the LSP count; None when there is no table."""
    text = (text or "").replace("\r", "")
    if "LSPID" not in text:
        return None
    # The LSPID is ``name.<pseudonode>-<fragment>``: a router's own bit is in pseudonode 00, fragment 00.
    routers = {match[1]: match[6] == "1" for match in _LSP.finditer(text) if match[2] == match[3] == "00"}
    total = _TOTAL.search(text)
    return {"routers": routers, "total": int(total[1]) if total else len(routers)} if routers else None


def _readers(case: CaseFile) -> list[str]:
    """Routers at the ends of the IS-IS adjacencies the spine found, in name order."""
    found: set[str] = set()
    for ev in case.evidence:
        if ev.get("kind") == "spine" and ev.get("role") == "isis":
            for edge in (ev.get("payload") or {}).get("operational_edges") or []:
                found.update(device for device, _ in _ends(str((edge or {}).get("id") or "")))
    return sorted(found)


async def record_isis_overload(client: Any, case: CaseFile, *, call: Any = None) -> None:
    """Ask a router for the link-state database and keep each router's overload bit with the scan."""
    readers = _readers(case)
    if not readers:
        return
    from diagnostic_mas.operational_checks.probe import Probe, safe
    from diagnostic_mas.operational_checks.scheduler import DeviceCalls, ProbeCache

    calls, cache = DeviceCalls(call or call_mcp, 1), ProbeCache()
    tried: list[str] = []
    parsed: dict[str, Any] | None = None
    for device in [name for name in readers if safe(name)][:TRIES]:
        tried.append(device)
        parsed = parse_isis_database(await Probe(client, calls, cache, max_calls=1).show(device, COMMAND, required=False))
        if parsed:
            break
    add_evidence(case, {"kind": KIND, "role": "underlay", "layer": "underlay", "payload": {
        "read_at": datetime.now(timezone.utc).isoformat(), "tried": tried,
        "read_on": tried[-1] if parsed else None,
        "routers": dict(sorted(parsed["routers"].items())) if parsed else {},
        "total": parsed["total"] if parsed else 0}})


def _saved(case: CaseFile) -> dict[str, Any] | None:
    found = [ev.get("payload") for ev in case.evidence if ev.get("kind") == KIND]
    return found[-1] if found and isinstance(found[-1], dict) else None


def isis_overload_leads(case: CaseFile) -> list[str]:
    """Routers that advertise the overload bit in this scan's database read, in name order."""
    return [name for name, bit in ((_saved(case) or {}).get("routers") or {}).items() if bit]


def overload_reads_line(case: CaseFile) -> str | None:
    """Where the database was read, how many routers it listed and how many set the bit, for the run details."""
    saved = _saved(case)
    if saved is None:
        return None
    if not saved.get("read_on"):
        return ("**IS-IS overload bit:** not read (asked " + ", ".join(f"`{name}`" for name in saved.get("tried") or [])
                + ")")
    routers, set_ = saved.get("routers") or {}, len(isis_overload_leads(case))
    return (f"**IS-IS overload bit:** read on `{saved['read_on']}`: {len(routers)} router{'s' * (len(routers) != 1)}, "
            + (f"{set_} set" if set_ else "none set"))
