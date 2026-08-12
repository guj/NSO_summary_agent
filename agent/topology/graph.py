"""Shim — implementation lives in nso_facts.topology.graph."""
from nso_facts.topology.graph import *  # noqa: F403
import nso_facts.topology.graph as _impl
globals().update(
    {name: getattr(_impl, name) for name in dir(_impl) if not name.startswith("__")}
)
