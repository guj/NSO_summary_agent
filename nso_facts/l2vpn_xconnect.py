"""Parse IOS-XR ``show l2vpn xconnect`` and match L2 service access circuits."""

from __future__ import annotations

import re
from typing import Any

from nso_facts.topology.interfaces import interfaces_match

# Group + name on their own line (e.g. "evpn_vpws  evpn_vpws_9001")
_GROUP_NAME = re.compile(
    r"^(?P<group>\S+)\s+(?P<name>\S+)\s*$"
)
# Status line: XC ST, AC desc, AC ST (Segment 2 parsed from remainder)
_STATUS_LINE = re.compile(
    r"^\s*(?P<st>UP|DN|AD|UR|SB|SR)\s+"
    r"(?P<ac>(?:Hu|FH|TF|Fo|Te|Gi|BE|BV|Nu|Mg|Lo)[\w/.\-]+)\s+"
    r"(?P<ac_st>UP|DN|AD|UR|SB|SR)\b"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)

_SEG2_ST = frozenset({"UP", "DN", "AD", "UR", "SB", "SR"})

_L2_SERVICE_TYPES = frozenset({"l2ptp", "l2sts"})


def is_l2_service_type(service_type: str) -> bool:
    return str(service_type or "").strip().lower() in _L2_SERVICE_TYPES


def parse_l2vpn_xconnect(text: str) -> list[dict[str, Any]]:
    """Return rows with keys: group, name, st, ac."""
    rows: list[dict[str, Any]] = []
    group = ""
    name = ""
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line or line.startswith("---") or "Legend:" in line:
            continue
        if "XConnect" in line and "Segment" in line:
            continue
        if "Group" in line and "Name" in line and "ST" in line:
            continue

        gm = _GROUP_NAME.match(line.strip())
        if gm and not line.strip().upper().startswith(
            ("UP", "DN", "AD", "UR", "SB", "SR")
        ):
            # Avoid treating "UP   Hu..." as group/name
            maybe_group = gm.group("group")
            if maybe_group.upper() not in {
                "UP",
                "DN",
                "AD",
                "UR",
                "SB",
                "SR",
            }:
                group = maybe_group
                name = gm.group("name")
                continue

        sm = _STATUS_LINE.match(line)
        if sm:
            row: dict[str, Any] = {
                "group": group,
                "name": name,
                "st": sm.group("st").upper(),
                "ac": sm.group("ac"),
                "ac_st": sm.group("ac_st").upper(),
            }
            rest = (sm.group("rest") or "").strip()
            if rest:
                # Trailing token is often Segment-2 ST (EVPN side)
                parts = rest.rsplit(None, 1)
                if len(parts) == 2 and parts[1].upper() in _SEG2_ST:
                    row["seg2"] = parts[0].strip()
                    row["seg2_st"] = parts[1].upper()
                else:
                    row["seg2"] = rest
            rows.append(row)
    return rows


def ac_name_from_endpoint(interface: dict[str, Any] | None) -> str | None:
    """Build short AC name from service YANG interface leaf."""
    if not isinstance(interface, dict):
        return None
    itype = interface.get("type")
    iid = interface.get("id")
    if not isinstance(itype, str) or not isinstance(iid, str) or not itype or not iid:
        return None
    long_name = f"{itype}{iid}"
    vlan = interface.get("outervlan")
    if vlan is not None and str(vlan) != "":
        long_name = f"{long_name}.{vlan}"
    # Prefer short form for matching show output
    from nso_facts.topology.interfaces import interface_variants

    variants = interface_variants(long_name)
    # Pick shortest Hu… style if present
    shorts = sorted(variants, key=len)
    return shorts[0] if shorts else long_name


def extract_l2_access_endpoints(instance: dict[str, Any]) -> list[dict[str, str]]:
    """Collect {device, ac} from site/stp-style ends (interface dict or list)."""
    out: list[dict[str, str]] = []
    if not isinstance(instance, dict):
        return out
    for _key, val in instance.items():
        if not isinstance(val, dict):
            continue
        device = val.get("device")
        if not isinstance(device, str) or not device:
            continue
        iface = val.get("interface")
        iface_list: list[Any]
        if isinstance(iface, dict):
            iface_list = [iface]
        elif isinstance(iface, list):
            iface_list = iface
        else:
            continue
        for one in iface_list:
            ac = ac_name_from_endpoint(one if isinstance(one, dict) else None)
            if not ac:
                continue
            out.append({"device": device, "ac": ac})
    return out


def find_xconnect_row(
    ac: str, rows: list[dict[str, Any]]
) -> dict[str, Any] | None:
    for row in rows:
        row_ac = str(row.get("ac") or "")
        if interfaces_match(ac, row_ac):
            return row
    return None


def summarize_live_l2(
    endpoints: list[dict[str, str]],
    rows_by_device: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Build live_l2 evidence (not used for hardcoded dataplane status)."""
    if not endpoints:
        return {"probed": False, "endpoints": [], "summary": "not_checked"}

    detailed: list[dict[str, Any]] = []
    states: list[str] = []

    for ep in endpoints:
        device = ep.get("device") or ""
        ac = ep.get("ac") or ""
        rows = rows_by_device.get(device)
        entry: dict[str, Any] = {"device": device, "ac": ac}
        if rows is None:
            entry["st"] = None
            entry["error"] = "no_xconnect_data"
            detailed.append(entry)
            continue
        row = find_xconnect_row(ac, rows)
        if row is None:
            entry["st"] = None
            entry["error"] = "ac_not_found"
            detailed.append(entry)
            continue
        st = str(row.get("st") or "").upper()
        entry["st"] = st
        entry["xconnect"] = row.get("name")
        entry["group"] = row.get("group")
        if row.get("ac_st"):
            entry["ac_st"] = str(row.get("ac_st")).upper()
        if row.get("seg2"):
            entry["seg2"] = row.get("seg2")
        if row.get("seg2_st"):
            entry["seg2_st"] = str(row.get("seg2_st")).upper()
        detailed.append(entry)
        if st:
            states.append(st)

    up = [s for s in states if s == "UP"]
    bad = [s for s in states if s and s != "UP"]

    if not states:
        summary = "unknown"
    elif bad and up:
        summary = "degraded"
    elif bad and not up:
        summary = "down"
    elif up and not bad:
        summary = "up"
    else:
        summary = "unknown"

    return {
        "probed": True,
        "endpoints": detailed,
        "summary": summary,
    }
