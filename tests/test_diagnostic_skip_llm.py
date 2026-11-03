"""Tests for nso-diagnostic-run --skip-llm without an LLM key or endpoint."""

from __future__ import annotations

import argparse
import urllib.error
import urllib.request
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.config import load_settings
from diagnostic_mas import run as run_mod


@pytest.fixture
def nso_env(monkeypatch, tmp_path):
    """NSO settings present; LLM key blank, as shipped in .env.example."""
    monkeypatch.setenv("NSO_ADDRESS", "192.0.2.1")
    monkeypatch.setenv("NSO_PASSWORD", "test")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FABRIC_AI_API_URL", "https://llm.invalid")
    monkeypatch.setenv("FABRIC_AI_API_KEY", "")


@pytest.fixture
def llm_requests(monkeypatch):
    """Record HTTP requests to the LLM endpoint and keep them off the network."""
    urls: list[str] = []

    def _urlopen(req, *args, **kwargs):
        urls.append(req.full_url)
        raise urllib.error.URLError("blocked in tests")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return urls


def _args(*, skip_llm: bool) -> argparse.Namespace:
    return argparse.Namespace(
        skip_llm=skip_llm,
        dry_run=True,
        publish=False,
        isis_only=False,
        bgp_only=False,
        device_only=False,
        service_only=False,
        skip_service=True,
        devices="",
        service_type="",
        service_id="",
        max_deep_checks=0,
        max_handoffs=0,
    )


class _Session:
    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *exc):
        return False


async def _run_offline(args: argparse.Namespace) -> int:
    """Run the real entry point with MCP collection and rendering stubbed."""
    stubs = {
        "mcp_session": MagicMock(return_value=_Session()),
        "call_mcp": AsyncMock(
            return_value={"status": "success", "data": {"device": []}}
        ),
        "parse_device_names": MagicMock(return_value=["pe1"]),
        "run_mandatory_spines": AsyncMock(),
        "render_report": MagicMock(return_value="# report\n"),
    }
    with ExitStack() as stack:
        for name, stub in stubs.items():
            stack.enter_context(patch.object(run_mod, name, new=stub))
        return await run_mod._run(args)


@pytest.mark.asyncio
async def test_skip_llm_run_needs_no_llm_key(nso_env, llm_requests):
    assert await _run_offline(_args(skip_llm=True)) == 0


@pytest.mark.asyncio
async def test_skip_llm_run_contacts_no_llm_endpoint(
    nso_env, llm_requests, monkeypatch
):
    monkeypatch.setenv("FABRIC_AI_API_KEY", "sk-test")

    assert await _run_offline(_args(skip_llm=True)) == 0
    assert llm_requests == []


@pytest.mark.asyncio
async def test_a_run_loads_the_port_findings_of_the_last_published_scan(
    nso_env, llm_requests, tmp_path, monkeypatch
):
    """A port the last scan explained can keep its finding: the run hands that scan to the port step."""
    from diagnostic_mas import port_investigation
    from diagnostic_mas.case import Budget, CaseFile
    from diagnostic_mas.state_paths import persist_case

    persist_case(tmp_path / "diagnostic_mas", run_id="20261102T120000Z", case=CaseFile(budget=Budget(0, 0)))
    loaded: list[str] = []
    monkeypatch.setattr(port_investigation, "previous_port_findings",
                        lambda previous: loaded.append((previous or {}).get("run_id")) or {})

    assert await _run_offline(_args(skip_llm=True)) == 0
    assert loaded == ["20261102T120000Z"]


@pytest.mark.asyncio
async def test_llm_run_still_requires_llm_key(nso_env, llm_requests):
    with pytest.raises(RuntimeError, match="FABRIC_AI_API_KEY is required"):
        await _run_offline(_args(skip_llm=False))


def test_load_settings_allows_blank_llm_key_when_not_required(nso_env):
    assert load_settings(require_llm_key=False).fabric_api_key == ""


def test_load_settings_requires_llm_key_by_default(nso_env):
    with pytest.raises(RuntimeError, match="FABRIC_AI_API_KEY is required"):
        load_settings()
