"""Shim — implementation lives in nso_facts.hardware_health."""
from nso_facts.hardware_health import *  # noqa: F403
import nso_facts.hardware_health as _impl
globals().update(
    {name: getattr(_impl, name) for name in dir(_impl) if not name.startswith("__")}
)
