"""One report group for services blocked by unresolved targeted sync retries."""
from diagnostic_mas.device_health import services_from_case


def sync_failure_group(case):
    failed = set()
    for ev in case.evidence:
        if ev.get("kind") == "spine" and ev.get("role") == "service":
            audit = ((ev.get("payload") or {}).get("extra") or {}).get("sync_rechecks") or {}
            failed.update(d for d, r in audit.items() if r.get("resolved") is False)
    from diagnostic_mas.service_presence import disappeared
    affected = {key for key, r in services_from_case(case).items()
                if not disappeared(r) and any(d in failed and value != "in-sync"
                    for d, value in (r.get("device_sync") or {}).items())}
    return sorted(failed), len(affected)


def format_sync_failure_group(case):
    devices, count = sync_failure_group(case)
    if not devices or not count:
        return []
    names = ", ".join(f"`{d}`" for d in devices)
    return ["### Sync verification failed", "",
            f"Devices: {names}. Fleet checks and targeted MCP retries did not establish synchronization.", "",
            f"**{count} distinct services remain Unknown.** Operational checks and routine LLM digs were skipped. This does not establish configuration drift or a device/service outage.", "",
            "Run these read-only commands in the NSO CLI and retain the complete responses:", "", "```text",
            *[f"devices device {d} check-sync" for d in devices], "```", "",
            "Compare the CLI responses with MCP results. Once sync verification succeeds, reassess the affected services.", ""]
