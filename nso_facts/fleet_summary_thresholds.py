"""Load CPU/memory alert thresholds for Fleet Summary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_CPU = 80
_DEFAULT_MEMORY = 85
_KEY_RE = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<val>-?\d+(?:\.\d+)?)\s*(?:#.*)?$"
)

_REPO_DEFAULT = (
    Path(__file__).resolve().parent.parent / "config" / "fleet_summary_thresholds.yaml"
)


@dataclass(frozen=True)
class FleetSummaryThresholds:
    cpu_five_min_pct: int = _DEFAULT_CPU
    memory_used_pct: int = _DEFAULT_MEMORY


def load_fleet_summary_thresholds(
    path: Path | None = None,
) -> FleetSummaryThresholds:
    """Load thresholds from YAML-ish file; missing/invalid → defaults."""
    target = path if path is not None else _REPO_DEFAULT
    if not target.is_file():
        return FleetSummaryThresholds()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return FleetSummaryThresholds()

    cpu = _DEFAULT_CPU
    mem = _DEFAULT_MEMORY
    found_cpu = False
    found_mem = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _KEY_RE.match(line)
        if not match:
            continue
        key = match.group("key")
        try:
            value = int(float(match.group("val")))
        except ValueError:
            continue
        if key == "cpu_five_min_pct":
            cpu = value
            found_cpu = True
        elif key == "memory_used_pct":
            mem = value
            found_mem = True

    if not found_cpu and not found_mem:
        # File present but unusable — still defaults
        return FleetSummaryThresholds()
    return FleetSummaryThresholds(cpu_five_min_pct=cpu, memory_used_pct=mem)
