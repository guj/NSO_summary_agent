"""Shim — implementation lives in nso_report.devices."""
from nso_report.devices import *  # noqa: F403
import nso_report.devices as _impl
globals().update(
    {name: getattr(_impl, name) for name in dir(_impl) if not name.startswith("__")}
)
