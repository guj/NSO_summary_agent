"""State helpers for diagnostic_mas runs."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent.config import Settings
from diagnostic_mas.case import CaseFile


def diagnostic_mas_state_dir(settings: Settings) -> Path:
    return Path(settings.state_dir) / "diagnostic_mas"


def case_to_dict(case: CaseFile) -> dict[str, Any]:
    return {
        "budget": asdict(case.budget),
        "evidence": case.evidence,
        "issues": case.issues,
        "diagnoses": case.diagnoses,
        "hypotheses": case.hypotheses,
        "plans": case.plans,
        "handoffs": case.handoffs,
        "device_names": list(case.device_names),
        "live_verified_devices": list(case.live_verified_devices),
        "focus_devices": list(case.focus_devices),
        "service_coverage": dict(case.service_coverage),
        "last_known_service_faults": list(case.last_known_service_faults),
    }


def persist_case(
    state_dir: Path,
    *,
    run_id: str,
    case: CaseFile,
    report: str | None = None,
) -> Path:
    state_dir = Path(state_dir)
    run_dir = state_dir / "runs" / str(run_id).replace(":", "-")
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "case.json"
    path.write_text(
        json.dumps({**case_to_dict(case), "run_id": run_id}, indent=2, default=str), encoding="utf-8"
    )
    report_path: Path | None = None
    if report is not None:
        report_path = run_dir / "report.md"
        report_path.write_text(report, encoding="utf-8")
        from diagnostic_mas.html_report import render_html_report

        (run_dir / "report.html").write_text(render_html_report(report, run_id), encoding="utf-8")
    latest = state_dir / "latest.json"
    meta: dict[str, Any] = {
        "run_id": run_id,
        "pipeline": "diagnostic-mas",
        "case_path": str(path),
    }
    if report_path is not None:
        meta["report_path"] = str(report_path)
        meta["report_html_path"] = str(run_dir / "report.html")
    latest.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path
