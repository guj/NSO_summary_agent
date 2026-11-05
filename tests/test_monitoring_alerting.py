"""The alert for scans that stopped arriving is opt-in and never puts a webhook URL in the files."""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

MONITORING = Path(__file__).resolve().parents[1] / "deploy/monitoring"
pytestmark = pytest.mark.skipif(not MONITORING.exists(), reason="deploy/ is not part of this checkout")


def _load(name: str) -> dict:
    return yaml.safe_load((MONITORING / name).read_text())


def _rule() -> dict:
    return _load("grafana/alerting/nso-scan-alerts.yaml")["groups"][0]["rules"][0]


def test_the_stack_without_the_alerting_file_mounts_no_alert():
    volumes = _load("docker-compose.yml")["services"]["grafana"]["volumes"]

    assert not any("alerting" in volume for volume in volumes)
    assert "./grafana/provisioning/datasources:/etc/grafana/provisioning/datasources:ro" in volumes


def test_the_alerting_file_adds_the_alert_and_requires_a_webhook():
    grafana = _load("docker-compose.alerting.yml")["services"]["grafana"]

    assert grafana["volumes"] == ["./grafana/alerting:/etc/grafana/provisioning/alerting:ro"]
    assert grafana["environment"]["NSO_ALERT_SLACK_WEBHOOK_URL"].startswith("${NSO_ALERT_SLACK_WEBHOOK_URL:?")


def test_the_alert_watches_published_scans_and_fires_after_48_hours_by_default():
    rule = _rule()
    query = next(step["model"]["expr"] for step in rule["data"] if step["refId"] == "A")
    threshold = next(step["model"] for step in rule["data"] if step["refId"] == rule["condition"])

    assert 'nso_summary_last_run_timestamp_seconds{pipeline="diagnostic"}' in query and "/ 3600" in query
    assert threshold["conditions"][0]["evaluator"] == {"type": "gt", "params": [48]}


def test_the_slack_webhook_comes_from_the_environment():
    receiver = _load("grafana/alerting/nso-scan-alerts.yaml")["contactPoints"][0]["receivers"][0]

    assert receiver["type"] == "slack" and receiver["settings"]["url"] == "$NSO_ALERT_SLACK_WEBHOOK_URL"
