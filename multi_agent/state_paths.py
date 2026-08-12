"""State directory helpers for the multi-agent production pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.config import Settings


def multi_agent_state_dir(settings: Settings) -> Path:
    return Path(settings.state_dir) / "multi_agent"


def multi_agent_run_dir(state_dir: Path, run_id: str) -> Path:
    safe_id = str(run_id).replace(":", "-")
    return Path(state_dir) / "runs" / safe_id


def load_previous_latest(state_dir: Path) -> dict[str, Any] | None:
    latest = Path(state_dir) / "latest.json"
    if not latest.is_file():
        return None
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_multi_agent_latest(state_dir: Path, latest: dict[str, Any]) -> Path:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "latest.json"
    path.write_text(json.dumps(latest, indent=2, default=str), encoding="utf-8")
    return path


def build_latest_payload(
    *,
    run_id: str,
    counts: dict[str, Any],
    services: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "pipeline": "multi-agent",
        "counts": counts,
        "services": services,
    }


def persist_multi_agent_run(
    state_dir: Path,
    *,
    run_id: str,
    counts: dict[str, Any],
    services: dict[str, Any],
    delta: dict[str, Any] | None,
    report: str,
    artifact_files: dict[str, str],
) -> Path:
    """Write runs/<id>/* and update latest.json.

    ``artifact_files`` maps filename -> file text (JSON or other).
    """
    run_dir = multi_agent_run_dir(state_dir, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    for name, content in artifact_files.items():
        (run_dir / name).write_text(content, encoding="utf-8")
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    if delta is not None:
        (run_dir / "delta.json").write_text(
            json.dumps(delta, indent=2, default=str), encoding="utf-8"
        )
    save_multi_agent_latest(
        state_dir,
        build_latest_payload(run_id=run_id, counts=counts, services=services),
    )
    return run_dir
