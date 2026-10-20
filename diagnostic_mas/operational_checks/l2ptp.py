"""L2PTP basic readiness: exact AC-matched xconnect on every endpoint, then the
service label followed in each direction.

The label one end shows for the far end can be a local entry that is sent as a
different label, so two ends showing different numbers is not a fault. Only a
followed label passes; anything else is unknown, never down.
"""
import re

from nso_facts.topology.interfaces import interfaces_match

from .common import result, sync_ready
from .probe import safe


def evaluate(record, *, force=False):
    if not sync_ready(record) and not force:
        return result(record, "not_checked", "Sync prerequisite not satisfied", [])
    endpoints = (record.get("live_l2") or {}).get("endpoints") or []
    checks = [{"check": "xconnect", "tool": "exec_show",
               "command": "l2vpn xconnect", "observation": dict(ep),
               "sources": [{"device": ep.get("device"), "command": "l2vpn xconnect"}],
               "status": ("unknown" if ep.get("error") else
                          "pass" if ep.get("st") == "UP" and ep.get("xconnect")
                          and all(ep.get(k) in (None, "", "UP") for k in ("ac_st", "seg2_st")) else
                          "fault" if ep.get("st") in {"DN", "AD", "UR"} or
                          any(ep.get(k) in {"DN", "AD", "UR"} for k in ("ac_st", "seg2_st")) else "unknown")}
              for ep in endpoints]
    if any(c["status"] == "fault" for c in checks):
        return result(record, "down", "Required endpoint xconnect is not operational", checks)
    devices = set(record.get("devices") or [])
    covered = {ep.get("device") for ep in endpoints if ep.get("ac")}
    if len(endpoints) >= 2 and devices and devices == covered and all(c["status"] == "pass" for c in checks):
        return result(record, "up", "All service endpoint xconnects are UP", checks)
    return result(record, "unknown", "Endpoint identity or xconnect evidence incomplete", checks)


def xc_labels(text, ac):
    """Service labels of the one circuit in a scoped ``l2vpn xconnect ... detail``, or None."""
    if len(re.findall(r"^Group \S+, XC \S+, state is ", text, re.M)) != 1:
        return None
    acs = re.findall(r"^\s+AC: ([^,\s]+), state is \w+", text, re.M)
    labels = re.findall(r"^\s+Label\s+(\d+)\s+(\d+)\s*$", text, re.M)
    if len(acs) != 1 or not interfaces_match(ac, acs[0]) or len(labels) != 1:
        return None
    evi = re.search(r"^\s+EVPN: neighbor \S+, PW ID: evi (\d+),", text, re.M)
    ac_id = re.search(r"^\s+AC ID\s+(\d+)\s+(\d+)\s*$", text, re.M)
    return {"evi": evi[1] if evi else None, "local": labels[0][0], "remote": labels[0][1],
            "ac_id": (ac_id[1], ac_id[2]) if ac_id else None}


def forwarded(text, label):
    """(outgoing label, prefix or ID) of a local label's one forwarding entry, or None."""
    rows = re.findall(rf"^{re.escape(label)}\s+(\S+)\s+(\S+)", text, re.M)
    return rows[0] if len(rows) == 1 else None


async def _label_paths(endpoints, probe):
    # Nothing to follow unless the circuit runs between two devices.
    if len(endpoints) != 2 or len({ep.get("device") for ep in endpoints}) != 2:
        return
    views, texts = {}, {}
    for ep in endpoints:
        device, group, name = ep.get("device"), ep.get("group"), ep.get("xconnect")
        if not (safe(group) and safe(name)):
            probe.check("service_labels", "unknown", "Xconnect group and name not established", device)
            continue
        text = await probe.show(device, f"l2vpn xconnect group {group} xc-name {name} detail")
        view = xc_labels(text, ep.get("ac") or "")
        if view:
            views[device], texts[device] = view, text
        elif text:
            probe.check("service_labels", "unknown", "Service labels not readable in the xconnect detail", device, read=text)
    if len(views) != 2:
        return
    (a, left), (b, right) = views.items()
    if left["evi"] != right["evi"] or (left["ac_id"] and right["ac_id"] and left["ac_id"] != right["ac_id"][::-1]):
        probe.check("service_labels", "unknown", "The two ends do not describe one circuit (EVI or AC IDs differ)",
                    read=[texts[a], texts[b]])
        return
    for src, dst in ((a, b), (b, a)):
        sent, want, evi = views[src]["remote"], views[dst]["local"], views[src]["evi"]
        direction = f"{src} -> {dst}: "
        details = [texts[src], texts[dst]]
        if sent == want:
            probe.check("label_path", "pass", direction + f"sender uses the receiver's label {want}", src, read=details)
            continue
        table = await probe.show(src, f"mpls forwarding labels {sent}", required=False)
        entry = forwarded(table, sent)
        if not entry or (evi and entry[1] != f"EVPN:{evi}"):
            probe.check("label_path", "unknown", direction + f"sender label {sent} differs from the receiver's "
                        f"label {want} and its forwarding entry was not read", src, read=details + [table])
        elif entry[0] == want:
            probe.check("label_path", "pass", direction + f"sender label {sent} is sent as {want}, "
                        "the receiver's label", src, read=details + [table])
        else:
            probe.check("label_path", "unknown", direction + f"sender label {sent} is sent as {entry[0]}; "
                        f"the receiver's label is {want}", src, read=details + [table])


async def evaluate_with_labels(record, probe, *, force=False):
    """Xconnect state first; when every end is up, the label path in each direction."""
    basic = evaluate(record, force=force)
    if basic["status"] != "up":
        return basic
    await _label_paths((record.get("live_l2") or {}).get("endpoints") or [], probe)
    gaps = [str(c["observation"]) for c in probe.checks if c["status"] != "pass"]
    reason = basic["reason"] + ("; service label followed in each direction" if probe.checks else "")
    # Built after the reads, so the check time is not earlier than its sources.
    out = result(record, "unknown" if gaps else "up", "; ".join(gaps[:4]) if gaps else reason,
                 basic["checks"] + probe.checks)
    out["sources"], out["tool_calls"] = probe.sources, probe.calls
    return out
