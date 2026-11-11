"""State helpers for diagnostic_mas runs."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent.config import Settings
from diagnostic_mas.case import CaseFile
from nso_facts.mcp_accounting import CALLS_FILE


def diagnostic_mas_state_dir(settings: Settings) -> Path:
    return Path(settings.state_dir) / "diagnostic_mas"


def case_to_dict(case: CaseFile) -> dict[str, Any]:
    from diagnostic_mas.drill import earlier_findings

    return {
        "budget": asdict(case.budget),
        "run_configuration": dict(case.run_configuration),
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
        "item_history": dict(case.item_history),
        "earlier_findings": earlier_findings(case),
        "earlier_port_findings": dict(case.earlier_port_findings),
        "llm_usage": dict(case.llm_usage),
        "mcp_calls": dict(case.mcp_calls),
        # The provider's own halt message stays in the log: the saved case keeps only that it halted.
        "llm_status": dict(case.llm_status) | ({"halted": True} if case.llm_halt_reason else {}),
    }


def run_folder(state_dir: Path, run_id: str) -> Path:
    """The folder a saved scan keeps its case and reports in."""
    return Path(state_dir) / "runs" / str(run_id).replace(":", "-")


def persist_case(
    state_dir: Path,
    *,
    run_id: str,
    case: CaseFile,
    report: str | None = None,
    nso_address: str | None = None,
    faults: dict[str, Any] | None = None,
) -> Path:
    state_dir = Path(state_dir)
    run_dir = run_folder(state_dir, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "case.json"
    path.write_text(
        json.dumps({**case_to_dict(case), "run_id": run_id}, indent=2, default=str), encoding="utf-8"
    )
    report_path: Path | None = None
    if report is not None:
        from agent.publish import mask_nso_address
        from diagnostic_mas.html_report import render_html_report

        # The reports are shared; case.json stays as collected, for the next scan's comparison.
        report_path = run_dir / "report.md"
        report_path.write_text(mask_nso_address(report, nso_address), encoding="utf-8")
        html = render_html_report(report, run_id, case=case_to_dict(case))
        (run_dir / "report.html").write_text(mask_nso_address(html, nso_address), encoding="utf-8")
    faults_path: Path | None = None
    if faults is not None:
        from agent.publish import mask_nso_address

        # For a script to read; its fields are described in docs/DIAGNOSTIC_RUNNER.md.
        faults_path = run_dir / "faults.json"
        faults_path.write_text(mask_nso_address(json.dumps(faults, indent=2, default=str), nso_address),
                               encoding="utf-8")
    latest = state_dir / "latest.json"
    meta: dict[str, Any] = {
        "run_id": run_id,
        "pipeline": "diagnostic-mas",
        "case_path": str(path),
    }
    if report_path is not None:
        meta["report_path"] = str(report_path)
        meta["report_html_path"] = str(run_dir / "report.html")
    if faults_path is not None:
        meta["faults_path"] = str(faults_path)
    latest.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path


# A saved scan's folder is named by its run id and holds what ``persist_case`` writes, and the
# scan's list of MCP calls.
_RUN_FOLDER = re.compile(r"\d{8}T\d{6}Z")
_RUN_FILES = ("case.json", "report.md", "report.html", "faults.json", CALLS_FILE)


def _run_time(name: str) -> datetime | None:
    try:
        return datetime.strptime(name, "%Y%m%dT%H%M%SZ") if _RUN_FOLDER.fullmatch(name) else None
    except ValueError:
        return None


def prune_saved_runs(state_dir: Path, *, run_id: str, keep_days: int) -> int:
    """Remove saved scans whose run is more than ``keep_days`` before ``run_id``; return how many.

    Only folders directly in ``runs/`` named exactly like a run id are candidates, never this
    scan's nor the one ``latest.json`` names (the next scan's comparison). Only the files a scan
    writes are removed, and the folder only when that leaves it empty, so anything added stays;
    a renamed folder is kept. A removal that fails is reported and never stops the scan.
    """
    now = _run_time(str(run_id))
    if keep_days <= 0 or now is None:
        return 0
    cutoff = now - timedelta(days=keep_days)
    keep = {str(run_id)}
    try:
        latest = json.loads((Path(state_dir) / "latest.json").read_text(encoding="utf-8"))
        keep.add(Path(str(latest.get("case_path") or "")).parent.name)
    except (OSError, ValueError, AttributeError):
        pass
    runs = Path(state_dir) / "runs"
    try:
        folders = sorted(runs.iterdir())
    except OSError:
        return 0
    removed = 0
    for folder in folders:
        when = _run_time(folder.name)
        if when is None or when >= cutoff or folder.name in keep or folder.is_symlink() or not folder.is_dir():
            continue
        try:
            files = [folder / name for name in _RUN_FILES
                     if (folder / name).is_file() and not (folder / name).is_symlink()]
            for path in files:
                path.unlink()
            if not any(folder.iterdir()):
                folder.rmdir()
            removed += bool(files)
        except OSError as exc:
            print(f"[saved scans] could not remove {folder.name}: {exc}", file=sys.stderr)
    if removed:
        print(f"[saved scans] Removed {removed} saved scan{'s' * (removed != 1)} older than "
              f"{keep_days} days from {runs}", file=sys.stderr)
    return removed
