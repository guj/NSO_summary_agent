"""Run deterministic type-specific checks with run-local shared observations."""
from . import generic, l2bridge, l2sts, l3rt, port_mirror
from .common import result, sync_ready
from .probe import Probe, attachments

CHECKS = {"l2bridge": l2bridge.check, "l2sts": l2sts.check, "l3rt": l3rt.check,
          "port-mirror": port_mirror.check}


async def evaluate(record, instance, client, call, cache, *, force=False):
    if not sync_ready(record) and not force:
        return result(record, "not_checked", "Sync prerequisite not satisfied", [])
    probe = Probe(client, call, cache)
    if record["service_type"] in {"l2bridge", "l2sts"}:
        actual = {ep["device"] for ep in attachments(instance)}
        if actual != set(record.get("devices") or []):
            probe.check("attachment_coverage", "unknown", "Not every intended device has a supported attachment identity")
    # A type without its own module still gets its interfaces looked at.
    interfaces_only = record["service_type"] not in CHECKS
    await CHECKS.get(record["service_type"], generic.check)(instance, probe)
    # Every required observation must pass; queries themselves never imply health.
    states = [check["status"] for check in probe.checks]
    status = "down" if "fault" in states else "up" if states and all(s == "pass" for s in states) else "unknown"
    reasons = [str(c["observation"]) for c in probe.checks if c["status"] != "pass"]
    reason = "; ".join(reasons[:4]) if reasons else "All required basic PE-side checks passed"
    out = result(record, status, reason, probe.checks)
    out["intent"] = {key: instance[key] for key in
        ("device", "interface", "site-a", "site-z", "gateway-ipv4", "gateway-ipv6", "external-access",
         "from-interface", "from-interface-vlan", "to-interface", "direction")
        if key in instance}
    if interfaces_only:
        out["coverage"] = "interfaces_only"
    out["sources"] = probe.sources
    out["tool_calls"] = probe.calls
    return out
