"""Shim — implementation lives in nso_facts.fleet_summary_thresholds."""
from nso_facts.fleet_summary_thresholds import *  # noqa: F403
import nso_facts.fleet_summary_thresholds as _impl
globals().update(
    {name: getattr(_impl, name) for name in dir(_impl) if not name.startswith("__")}
)
