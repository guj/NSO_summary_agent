"""Shim — implementation lives in nso_facts.topology.iface_equiv."""
from nso_facts.topology.iface_equiv import *  # noqa: F403
import nso_facts.topology.iface_equiv as _impl
globals().update(
    {name: getattr(_impl, name) for name in dir(_impl) if not name.startswith("__")}
)
