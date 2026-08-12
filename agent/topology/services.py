"""Shim — implementation lives in nso_facts.topology.services."""
from nso_facts.topology.services import *  # noqa: F403
import nso_facts.topology.services as _impl
globals().update(
    {name: getattr(_impl, name) for name in dir(_impl) if not name.startswith("__")}
)
