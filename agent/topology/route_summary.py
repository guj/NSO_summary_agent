"""Shim — implementation lives in nso_facts.topology.route_summary."""
from nso_facts.topology.route_summary import *  # noqa: F403
import nso_facts.topology.route_summary as _impl
globals().update(
    {name: getattr(_impl, name) for name in dir(_impl) if not name.startswith("__")}
)
