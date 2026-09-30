"""Local L2Bridge readiness. Remote/gateway dependencies require investigation."""
import re
from .bridge import collect


async def check(instance, probe):
    domains = await collect(instance, probe)
    for device, domain in domains.items():
        text = domain["detail"]
        # A local bridge fast path cannot certify a remote or routed topology.
        if domain["evi"] or re.search(r"\bBVI\d+|PWs: [1-9]|VFIs: [1-9]|VNIs: [1-9]", text):
            probe.check("additional_dependencies", "unknown", "Remote or gateway dependency requires scoped investigation", device)
        if re.search(r"Split Horizon Group: (?!none)[^\n]+|E-Tree: Leaf", text, re.I):
            probe.check("switching_policy", "unknown", "Non-default switching restrictions require investigation", device)
    if len(domains) != 1:
        probe.check("bridge_scope", "unknown", "Local bridge fast path requires exactly one verified device/domain")
