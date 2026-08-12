"""Shim — implementation lives in nso_facts.metrics."""
from nso_facts.metrics import *  # noqa: F403
import nso_facts.metrics as _impl
globals().update(
    {name: getattr(_impl, name) for name in dir(_impl) if not name.startswith("__")}
)
