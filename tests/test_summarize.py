from pathlib import Path
from unittest.mock import patch

from agent.config import Settings


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


def test_fabric_openai_client_uses_60s_and_no_retry():
    from agent.summarize import (
        FABRIC_CHAT_MAX_RETRIES,
        FABRIC_CHAT_TIMEOUT_SEC,
        fabric_openai_client,
    )

    assert FABRIC_CHAT_TIMEOUT_SEC == 60.0
    assert FABRIC_CHAT_MAX_RETRIES == 0
    client = fabric_openai_client(_settings())
    assert client.max_retries == 0
    # httpx.Timeout: read bound is the per-attempt limit
    assert float(client.timeout.read) == 60.0
