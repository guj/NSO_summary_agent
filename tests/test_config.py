"""Tests for configuration parsing."""

from agent.config import (
    DEFAULT_IGNORE_SERVICE_TYPES,
    DEFAULT_REPORT_SECTIONS,
    _parse_ignore_service_types,
    _parse_report_sections,
)
from nso_facts.service_collect import (
    is_ignored_service_type as _is_ignored_service_type,
    select_service_types as _select_service_types,
)


def test_report_sections_default_when_unset():
    assert _parse_report_sections(None) == DEFAULT_REPORT_SECTIONS
    assert DEFAULT_REPORT_SECTIONS == (
        "executive",
        "devices",
        "ignored_types",
    )


def test_report_sections_custom_order_and_case():
    assert _parse_report_sections("Devices, COUNTS") == ("devices", "counts")


def test_report_sections_skips_unknown(capsys):
    assert _parse_report_sections("problems,bogus,delta") == ("problems", "delta")
    err = capsys.readouterr().err
    assert "bogus" in err.lower() or "unknown" in err.lower()


def test_report_sections_empty_falls_back_to_default(capsys):
    assert _parse_report_sections("") == DEFAULT_REPORT_SECTIONS
    assert _parse_report_sections(" , , ") == DEFAULT_REPORT_SECTIONS
    err = capsys.readouterr().err
    assert err  # warned


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
