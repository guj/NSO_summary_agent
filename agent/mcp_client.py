"""Shim — implementation lives in nso_facts.mcp_client."""
from nso_facts.mcp_client import *  # noqa: F403
import nso_facts.mcp_client as _impl
globals().update(
    {name: getattr(_impl, name) for name in dir(_impl) if not name.startswith("__")}
)
