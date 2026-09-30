"""Reconcile saved basic replication checks with scoped dig transport evidence."""
from .probe import cef_ready


def replication_ready(basic, devices, evidence):
    """Require every basic component; only transport Unknown may be closed here.

    Caller validates service identity and collector provenance. This consumes
    existing normalized checks, never the model's prose or a guessed command.
    """
    checks = basic.get("checks") or []
    required = {"attachment", "bridge_state", "membership", "encapsulation",
                "local_forwarding", "local_flooding", "evpn_binding", "effective_evpn"}
    views = {}
    for device in devices:
        local = [c for c in checks if c.get("device") == device]
        if not required <= {c.get("check") for c in local if c.get("status") == "pass"}:
            return False
        effective = [c.get("observation") for c in local if c.get("check") == "effective_evpn"]
        if len(effective) != 1 or not isinstance(effective[0], dict):
            return False
        views[device] = effective[0]
    # Unknown identity, local forwarding, replication, or any fault must remain
    # unresolved. A raw transport response can close only a transport Unknown.
    if any(c.get("status") != "pass" and not
           (c.get("check") == "transport" and c.get("status") == "unknown") for c in checks):
        return False
    for src in devices:
        for dst in devices:
            if src == dst:
                continue
            prefix = str(views[dst].get("peer") or "") + "/32"
            if not views[dst].get("label") or views[src].get("evi") != views[dst].get("evi"):
                return False
            direction = f"{src} -> {dst}:"
            scoped = [c for c in checks if c.get("device") == src and
                      str(c.get("observation") or "").startswith(direction)]
            for name in ("rt_compatibility", "replication", "transport"):
                matches = [c for c in scoped if c.get("check") == name]
                if len(matches) != 1:
                    return False
                if name != "transport" and matches[0].get("status") != "pass":
                    return False
                if name == "transport":
                    # Use the latest exact-target dig result, if present; never
                    # allow an older basic pass to hide a contradictory result.
                    found, text = False, ""
                    for ev in evidence:
                        p = ev.get("payload") or {}
                        args = p.get("args") or {}
                        command = str(args.get("input_command") or args.get("command") or "").strip()
                        if command.startswith("show "):
                            command = command[5:]
                        if (ev.get("kind") != "drill" or p.get("check") != "exec_show"
                                or (args.get("device_name") or args.get("device")) != src
                                or command != f"cef {prefix}"):
                            continue
                        found = True
                        value = p.get("result")
                        while isinstance(value, dict):
                            value = value.get("data", value.get("result"))
                        text = value.replace("\r", "") if isinstance(value, str) and not p.get("error") else ""
                    if found:
                        if not cef_ready(text, prefix, labeled=True):
                            return False
                    elif matches[0].get("status") != "pass":
                        return False
    return len(devices) >= 2
