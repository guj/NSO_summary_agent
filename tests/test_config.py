"""Tests for configuration parsing."""

from agent.config import (
    DEFAULT_IGNORE_SERVICE_TYPES,
    _parse_ignore_service_types,
)
from nso_facts.service_collect import (
    is_ignored_service_type as _is_ignored_service_type,
    select_service_types as _select_service_types,
)


def test_ignore_service_types_default():
    assert _parse_ignore_service_types(None) == frozenset(DEFAULT_IGNORE_SERVICE_TYPES)


def test_ignore_service_types_empty_env_disables_filter():
    assert _parse_ignore_service_types("") == frozenset()


def test_ignore_service_types_csv():
    assert _parse_ignore_service_types("idipa, port-mirror") == frozenset(
        {"idipa", "port-mirror"}
    )


def test_mcp_args_verify_default_and_lab_override(monkeypatch):
    from agent.config import _mcp_args

    monkeypatch.setenv("NSO_ADDRESS", "192.0.2.1")
    monkeypatch.delenv("NSO_VERIFY", raising=False)
    monkeypatch.delenv("NSO_CA_BUNDLE", raising=False)
    monkeypatch.delenv("NSO_TIMEOUT", raising=False)
    args = _mcp_args()
    assert "--nso-verify" in args
    assert "--no-nso-verify" not in args
    assert "--nso-timeout=10" in args

    monkeypatch.setenv("NSO_TIMEOUT", "20")
    args = _mcp_args()
    assert "--nso-timeout=20" in args

    monkeypatch.setenv("NSO_VERIFY", "0")
    args = _mcp_args()
    assert "--no-nso-verify" in args
    assert "--nso-verify" not in args

    monkeypatch.setenv("NSO_VERIFY", "1")
    monkeypatch.setenv("NSO_CA_BUNDLE", "/tmp/ca.pem")
    args = _mcp_args()
    assert "--nso-verify" in args
    assert "--nso-ca-bundle=/tmp/ca.pem" in args


def test_select_service_types_filters_idipa():
    raw = [
        "/ncs:services/l2ptp:l2ptp",
        "/ncs:services/idipa:idipa",
        "/ncs:services/l3rt:l3rt",
    ]
    selected = _select_service_types(raw, frozenset({"idipa"}))
    assert selected == ["/ncs:services/l2ptp:l2ptp", "/ncs:services/l3rt:l3rt"]


def test_is_ignored_matches_module_or_service_name():
    assert _is_ignored_service_type("/ncs:services/idipa:idipa", frozenset({"idipa"}))
    assert not _is_ignored_service_type(
        "/ncs:services/l2ptp:l2ptp", frozenset({"idipa"})
    )


def test_mcp_results_keep_a_week_unless_set():
    import pytest
    from agent.config import _parse_keep_days

    assert _parse_keep_days(None) == 7
    assert _parse_keep_days("0") == 0
    assert _parse_keep_days("14") == 14
    for raw in ("", "-1", "1.5", "no"):
        with pytest.raises(ValueError, match="MCP_RESULTS_KEEP_DAYS"):
            _parse_keep_days(raw)


def test_notification_digest_is_off_unless_set(monkeypatch):
    from agent.config import load_settings

    for name, value in {"NSO_ADDRESS": "localhost", "NSO_PASSWORD": "test", "FABRIC_AI_API_KEY": "test"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("NOTIFICATION_DIGEST", raising=False)
    assert load_settings().notification_digest is False
    monkeypatch.setenv("NOTIFICATION_DIGEST", "1")
    assert load_settings().notification_digest is True


def test_saved_scans_are_kept_unless_runs_keep_days_is_set(monkeypatch):
    import pytest
    from agent.config import _parse_keep_days, load_settings

    for name, value in {"NSO_ADDRESS": "localhost", "NSO_PASSWORD": "test", "FABRIC_AI_API_KEY": "test"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("RUNS_KEEP_DAYS", raising=False)
    assert load_settings().runs_keep_days == 0
    monkeypatch.setenv("RUNS_KEEP_DAYS", "30")
    assert load_settings().runs_keep_days == 30
    with pytest.raises(ValueError, match="RUNS_KEEP_DAYS"):
        _parse_keep_days("-1", name="RUNS_KEEP_DAYS", default=0)
