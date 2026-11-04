"""Fixtures shared by the offline start-up tests."""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest


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
