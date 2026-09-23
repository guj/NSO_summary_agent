"""Tests for configuration parsing."""

from agent.config import (
    DEFAULT_IGNORE_SERVICE_TYPES,
    DEFAULT_REPORT_SECTIONS,
    _parse_ignore_service_types,
    _parse_report_sections,
)
from agent.collect import _is_ignored_service_type, _select_service_types


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


def test_max_service_types_default_and_cli_help():
    from agent.config import DEFAULT_MAX_SERVICE_TYPES
    from agent.run import main
    import io
    from contextlib import redirect_stdout, redirect_stderr
    from unittest.mock import patch

    assert DEFAULT_MAX_SERVICE_TYPES == 10
    buf = io.StringIO()
    with patch("sys.argv", ["nso-summary-run", "--help"]):
        with redirect_stdout(buf), redirect_stderr(buf):
            try:
                main()
            except SystemExit as exc:
                assert exc.code == 0
    help_text = buf.getvalue()
    assert "--max-service-types" in help_text
    assert "MAX_SERVICE_TYPES" in help_text


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
