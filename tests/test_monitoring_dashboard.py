"""The Grafana dashboard must only query metrics that a scan actually pushes."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from diagnostic_mas.metrics import metrics_snapshot_from_case
from nso_facts.metrics import build_phase1_metrics
from test_diagnostic_mas_metrics import scan_case

DASHBOARD = (Path(__file__).resolve().parents[1]
             / "deploy/monitoring/grafana/dashboards/nso-diagnostic.json")
pytestmark = pytest.mark.skipif(
    not DASHBOARD.exists(), reason="deploy/ is not part of this checkout")


def _queries(panels: list) -> list[str]:
    found: list[str] = []
    for panel in panels:
        found += [t["expr"] for t in panel.get("targets") or [] if t.get("expr")]
        found += _queries(panel.get("panels") or [])
    return found


def test_dashboard_queries_only_metrics_a_scan_pushes():
    snapshot = metrics_snapshot_from_case(scan_case(), run_id="run-1", report_url="")
    pushed = {
        re.match(r"[a-z0-9_]+", line).group(0)
        for line in build_phase1_metrics(snapshot, success=True, duration_seconds=1.0,
                                         pipeline="diagnostic")
        if not line.startswith("#")
    }
    queries = _queries(json.loads(DASHBOARD.read_text())["panels"])

    assert queries, "the dashboard has no queries"
    asked = {name for query in queries for name in re.findall(r"\bnso_[a-z0-9_]+", query)}
    assert asked - pushed == set()
