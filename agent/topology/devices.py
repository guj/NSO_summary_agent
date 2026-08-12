"""Shim — implementation lives in nso_facts.topology.devices."""
from nso_facts.topology.devices import *  # noqa: F403
import nso_facts.topology.devices as _impl
globals().update(
    {name: getattr(_impl, name) for name in dir(_impl) if not name.startswith("__")}
)
