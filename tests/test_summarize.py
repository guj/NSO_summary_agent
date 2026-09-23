from pathlib import Path
from unittest.mock import patch

from agent.config import Settings
from agent.summarize import _problems_context, summarize_report


def _settings(**overrides) -> Settings:
    defaults = {
        "mcp_server_cmd": "x",
        "mcp_server_args": [],
        "mcp_env": {},
        "fabric_api_key": "k",
        "fabric_api_url": "https://example.com",
        "fabric_model": "m",
        "state_dir": Path("/tmp"),
        "dry_run": False,
        "ignore_service_types": frozenset(),
        "max_service_types": 10,
        "report_sections": (
            "problems",
            "counts",
            "delta",
            "fleet_sync",
            "devices",
            "ignored_types",
        ),
        "slack_webhook_url": None,
        "smtp_host": None,
        "smtp_port": 587,
        "smtp_user": None,
        "smtp_password": None,
        "smtp_use_tls": True,
        "email_from": None,
        "email_to": [],
        "email_subject_prefix": "NSO Summary",
        "prometheus_pushgateway_url": None,
        "prometheus_job": "nso-summary",
        "prometheus_instance": "default",
        "prompts_dir": Path("/tmp"),
        "topology_force_update": False,
        "interface_equivalences_file": None,
        "iface_troubleshoot_max_tool_rounds": 10,
        "iface_troubleshoot_disable": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_problems_context_includes_interface_mismatches():
    snapshot = {
        "run_id": "r1",
        "counts": {},
        "services": {},
        "fleet_sync": None,
        "topology": {
            "operational": {
                "issues": [
                    {
                        "code": "config_live_mismatch",
                        "device": "renc",
                        "nso": "FourHundredGigE0/0/0/32",
                        "candidates": ["Hu0/0/0/32"],
                        "kind": "type_change",
                        "confirmed": False,
                        "box": None,
                        "message": "renc FourHundredGigE0/0/0/32: candidates: Hu0/0/0/32",
                    },
                    {
                        "code": "unexpected_live_object",
                        "message": "ignore me",
                    },
                ]
            }
        },
    }
    ctx = _problems_context(snapshot)
    assert len(ctx["interface_mismatches"]) == 1
    assert ctx["interface_mismatches"][0]["nso"] == "FourHundredGigE0/0/0/32"


def test_summarize_executive_uses_llm_when_section_present():
    settings = _settings(report_sections=("executive", "devices"))
    snapshot = {
        "run_id": "2026-07-15T16:57:00Z",
        "counts": {"l3rt": {"total": 1, "up": 1, "down": 0, "degraded": 0, "unknown": 0}},
        "topology": {},
        "fleet_sync": None,
        "system_health": {},
    }
    delta = {"first_run": True, "counts": {}}

    class _Msg:
        content = (
            "Overall Status\n--------------\n🟢 Services: Healthy\n\n"
            "Action Items\n------------\nNone reported."
        )

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            messages = kwargs.get("messages") or []
            system = ""
            for m in messages:
                if m.get("role") == "system":
                    system = m.get("content") or ""
            if "Operational Assessment" in system or "short Operational Assessment" in system:
                msg = _Msg()
                msg.content = (
                    "The fleet is stable with all services up; review NSO↔device "
                    "interface mappings before relying on interface status."
                )
                ch = _Choice()
                ch.message = msg
                resp = _Resp()
                resp.choices = [ch]
                return resp
            msg = _Msg()
            msg.content = (
                "Overall Status\n--------------\n🟢 Services: Healthy\n\n"
                "Action Items\n------------\nNone reported."
            )
            ch = _Choice()
            ch.message = msg
            resp = _Resp()
            resp.choices = [ch]
            return resp

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    with patch("agent.summarize.fabric_openai_client", return_value=_Client()):
        out = summarize_report(snapshot, delta, settings)
    assert "NSO Operations Snapshot" in out.markdown
    assert "🟢 Services: Healthy" in out.markdown
    assert "Operational Assessment" in out.markdown
    assert "fleet is stable" in out.markdown
    assert out.markdown.index("Device Health") < out.markdown.index(
        "Operational Assessment"
    )
    assert out.markdown.index("Operational Assessment") < out.markdown.index(
        "Detailed Device Analysis"
    )
    assert "Problems / Failures" not in out.markdown
    assert "Detailed Device Analysis" in out.markdown
    assert "OK Services" in out.plain


def test_summarize_skips_llm_when_problems_and_executive_omitted():
    settings = _settings(report_sections=("counts", "delta"))
    snapshot = {
        "run_id": "r1",
        "counts": {},
        "topology": {},
        "fleet_sync": None,
    }
    delta = {"first_run": True, "counts": {}}

    with patch("agent.summarize.fabric_openai_client") as client_factory:
        out = summarize_report(snapshot, delta, settings)
        client_factory.assert_not_called()
    assert "Problems / Failures" not in out.markdown
    assert "Service Counts" in out.markdown


def test_summarize_system_health_uses_separate_llm_call():
    settings = _settings(report_sections=("system_health", "counts"))
    snapshot = {
        "run_id": "r1",
        "counts": {},
        "topology": {},
        "fleet_sync": None,
        "system_health": {
            "sw1": {
                "cpu": {"one_min": 1, "five_min": 1, "fifteen_min": 1},
                "memory": {"total_mb": 100, "available_mb": 50, "used_pct": 50.0},
            }
        },
    }
    delta = {"first_run": True, "counts": {}}

    class _Msg:
        content = "**Infrastructure Health**\n\nFleet CPU is quiet; memory about half used on sw1."

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    with patch("agent.summarize.fabric_openai_client", return_value=_Client()) as factory:
        out = summarize_report(snapshot, delta, settings)
        factory.assert_called()
    assert out.markdown.count("**Infrastructure Health**") == 1
    assert "Fleet CPU is quiet" in out.markdown
    assert "Problems / Failures" not in out.markdown


def test_summarize_strips_duplicate_problems_heading():
    settings = _settings(report_sections=("problems", "counts"))
    snapshot = {
        "run_id": "r1",
        "counts": {},
        "services": {},
        "topology": {},
        "fleet_sync": None,
    }
    delta = {"first_run": True, "counts": {}}

    class _Msg:
        content = "**Problems / Failures**\n\n- iface down on sw1"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    with patch("agent.summarize.fabric_openai_client", return_value=_Client()):
        out = summarize_report(snapshot, delta, settings)
    assert out.markdown.count("**Problems / Failures**") == 1
    assert "- iface down on sw1" in out.markdown


def test_strip_section_heading_removes_llm_echo():
    from agent.summarize import _strip_section_heading

    assert _strip_section_heading(
        "**Infrastructure Health**\n\nAll quiet.",
        ("Infrastructure Health",),
    ) == "All quiet."
    assert _strip_section_heading(
        "Problems / Failures\n- iface down",
        ("Problems / Failures", "Problems"),
    ) == "- iface down"
    assert (
        _strip_section_heading("Already body only.", ("Infrastructure Health",))
        == "Already body only."
    )


def test_fabric_openai_client_uses_60s_and_one_retry():
    from agent.summarize import (
        FABRIC_CHAT_MAX_RETRIES,
        FABRIC_CHAT_TIMEOUT_SEC,
        fabric_openai_client,
    )

    assert FABRIC_CHAT_TIMEOUT_SEC == 60.0
    assert FABRIC_CHAT_MAX_RETRIES == 1
    client = fabric_openai_client(_settings())
    assert client.max_retries == 1
    # httpx.Timeout: read bound is the per-attempt limit
    assert float(client.timeout.read) == 60.0
