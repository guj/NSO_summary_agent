"""Shim — implementation lives in nso_facts.topology.persist."""
from nso_facts.topology.persist import *  # noqa: F403
import nso_facts.topology.persist as _impl
globals().update(
    {name: getattr(_impl, name) for name in dir(_impl) if not name.startswith("__")}
)
