"""Shim — implementation lives in nso_facts.delta."""
from nso_facts.delta import *  # noqa: F403
import nso_facts.delta as _impl
globals().update(
    {name: getattr(_impl, name) for name in dir(_impl) if not name.startswith("__")}
)
