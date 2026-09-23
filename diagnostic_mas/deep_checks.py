"""Gated deep-check execution for diagnostic_mas (reuse multi_agent)."""

from __future__ import annotations

from typing import Any

from multi_agent.base import BGP_ALLOWLIST, DEVICE_ALLOWLIST, ISIS_ALLOWLIST
from multi_agent.deep_checks import run_deep_checks

SERVICE_ALLOWLIST = frozenset({"exec_show", "check_service_sync", "get_interface_health"})

DRILL_ALLOWLIST = frozenset(
    {
        "exec_show",
        "get_interface_health",
        "check_service_sync",
        "get_hardware_health",
    }
)

# Read-only tools for dataplane LLM (matches what interactive Claude used for l2sts).
DATAPLANE_ALLOWLIST = frozenset(
    {
        "exec_show",
        "get_interface_health",
        "check_service_sync",
        "get_services",
        "explore_nso_path",
        "get_device_config",
        "compare_service_config",
        "compare_device_config",
    }
)

ROLE_ALLOWLISTS = {
    "isis": ISIS_ALLOWLIST,
    "bgp": BGP_ALLOWLIST,
    "device": DEVICE_ALLOWLIST,
    "service": SERVICE_ALLOWLIST,
}


async def run_role_deep_checks(
    client: Any,
    role: str,
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    allow = ROLE_ALLOWLISTS.get(role, frozenset())
    gated = [t for t in plan if str(t.get("check") or "") in allow]
    if not gated:
        return []
    return await run_deep_checks(client, gated)


def any_probe_succeeded(results: list[Any] | None) -> bool:
    """True if at least one probe returned without an ``error`` field.

    A nonempty list of error-only dicts must not count as success
    (``bool(results)`` is True for that case and incorrectly marks Issues explained).
    """
    for item in results or []:
        if isinstance(item, dict) and not item.get("error"):
            return True
    return False
