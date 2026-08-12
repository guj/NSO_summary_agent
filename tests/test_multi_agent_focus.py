"""Tests for multi-agent IS-IS / BGP layer focus flags."""

from __future__ import annotations

import pytest

from multi_agent.focus import resolve_layer_focus
from multi_agent.run import main


def test_resolve_layer_focus_default_runs_both():
    assert resolve_layer_focus() == (True, True)


def test_resolve_layer_focus_isis_only():
    assert resolve_layer_focus(isis_only=True) == (True, False)


def test_resolve_layer_focus_bgp_only():
    assert resolve_layer_focus(bgp_only=True) == (False, True)


def test_resolve_layer_focus_both_raises():
    with pytest.raises(ValueError, match="mutually exclusive"):
        resolve_layer_focus(isis_only=True, bgp_only=True)


def test_cli_rejects_both_only_flags():
    with pytest.raises(SystemExit) as exc:
        main(["--isis-only", "--bgp-only"])
    assert exc.value.code != 0
