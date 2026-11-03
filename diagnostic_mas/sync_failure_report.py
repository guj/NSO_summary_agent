"""One report group for services blocked by unresolved targeted sync retries."""
from diagnostic_mas.device_health import services_from_case


def _sync_audits(case):
    for ev in case.evidence:
        if ev.get("kind") == "spine" and ev.get("role") == "service":
            yield ((ev.get("payload") or {}).get("extra") or {}).get("sync_rechecks") or {}


def _affected(case, failed):
    """Each service still Unknown on a failed device, with those devices."""
    from diagnostic_mas.service_presence import disappeared
    out = {}
    for key, r in services_from_case(case).items():
        devices = {d for d, value in (r.get("device_sync") or {}).items() if d in failed and value != "in-sync"}
        if devices and not disappeared(r):
            out[key] = devices
    return out


def nso_unconnected(case, devices):
    """The devices whose quarantine says NSO itself could not connect to them."""
    from nso_facts.mcp_client import nso_could_not_connect
    messages = {str((i.get("devices") or [""])[0]): str(i.get("message") or "")
                for i in case.issues if str(i.get("code") or "") == "device_live_unreachable"}
    return sorted(d for d in devices if nso_could_not_connect(messages.get(d, "")))


def sync_failure_group(case):
    failed = set()
    for audit in _sync_audits(case):
        failed.update(d for d, r in audit.items() if r.get("resolved") is False)
    return sorted(failed), len(_affected(case, failed))


def sync_failure_split(case):
    """``(unconnected, their services, the other devices, theirs)``; a service on both counts as unconnected."""
    failed, _ = sync_failure_group(case)
    unconnected = nso_unconnected(case, failed)
    affected = _affected(case, set(failed))
    behind = sum(1 for devices in affected.values() if devices & set(unconnected))
    return unconnected, behind, [d for d in failed if d not in unconnected], len(affected) - behind


def _names(devices):
    quoted = [f"`{d}`" for d in devices]
    return ", ".join(quoted[:-1]) + " and " + quoted[-1] if len(quoted) > 1 else quoted[0]


def unconnected_followup(devices, count):
    """The follow-up for services left Unknown behind devices NSO could not connect to."""
    many = len(devices) > 1
    commands = "; ".join(f"`devices device {d} connect`" for d in devices)
    return (f"Check NSO's management connection to {_names(devices)} to settle {count} service{'s' if count != 1 else ''} "
            f"left Unknown: NSO's own attempt{'s' if many else ''} to connect to {'these devices' if many else 'the device'} "
            f"failed, so {'their' if many else 'its'} sync could not be read; {commands} in the NSO CLI "
            f"show{'' if many else 's'} whether NSO can reach {'them' if many else 'it'}; run `check-sync` once "
            f"{'they connect' if many else 'it connects'}. These are not confirmed outages.")


def format_sync_failure_group(case):
    devices, count = sync_failure_group(case)
    if not devices or not count:
        return []
    names = ", ".join(f"`{d}`" for d in devices)
    skipped = sorted({d for audit in _sync_audits(case) for d, r in audit.items() if d in devices and r.get("skipped")})
    if not skipped:
        retries = "Fleet checks and targeted MCP retries did not establish synchronization."
    elif len(skipped) < len(devices):
        retries = (f"Fleet checks and targeted MCP retries did not establish synchronization "
                   f"(the retry was skipped on {_names(skipped)}, quarantined this run).")
    else:
        retries = "Fleet checks did not establish synchronization; the targeted MCP retry was skipped (quarantined this run)."
    unconnected = nso_unconnected(case, devices)
    rest = [d for d in devices if d not in unconnected]
    lines = ["### Sync verification failed", "",
             f"Devices: {names}. {retries}", "",
             f"**{count} distinct services remain Unknown.** Operational checks and routine LLM digs were skipped. This does not establish configuration drift or a device/service outage.", ""]
    if unconnected:
        many = len(unconnected) > 1
        lines += [f"NSO itself could not connect to {_names(unconnected)} this run, so {'their' if many else 'its'} sync could not be read. "
                  f"These NSO CLI commands show whether NSO can reach {'them' if many else 'it'}; run `check-sync` once {'they connect' if many else 'it connects'}:",
                  "", "```text", *[f"devices device {d} connect" for d in unconnected], "```", ""]
    if rest:
        lines += ["Run these read-only commands in the NSO CLI and retain the complete responses:", "", "```text",
                  *[f"devices device {d} check-sync" for d in rest], "```", ""]
    lines += ["Compare the CLI responses with MCP results. Once sync verification succeeds, reassess the affected services.", ""]
    return lines
