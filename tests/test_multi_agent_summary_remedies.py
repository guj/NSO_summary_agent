"""Tests for multi-agent LLM summary payload (assessment + remedies)."""

from __future__ import annotations

from pathlib import Path

from multi_agent.orchestrator import _compact_evidence, summary_user_payload


def test_summary_prompt_asks_for_remedies():
    text = (
        Path(__file__).resolve().parents[1]
        / "multi_agent"
        / "prompts"
        / "summary.txt"
    ).read_text(encoding="utf-8")
    assert "Suggested remedies (hypotheses)" in text
    assert "Do not invent" in text or "do not invent" in text.lower()


def test_summary_user_payload_includes_compact_evidence():
    merged = {
        "issues_total": 1,
        "issues": [
            {
                "code": "unknown_neighbor_address",
                "message": "lbnl-data-sw 10.148.0.1: could not map",
            }
        ],
        "fleet": {},
        "agents": {
            "bgp": {
                "layer": "routing",
                "operational_summary": {"total": 3, "up": 3},
                "issues": [{"code": "unknown_neighbor_address"}],
                "evidence": [
                    {
                        "check": "verify_bgp_peer_reachability",
                        "reason": "unknown neighbor address",
                        "args": {"device_name": "lbnl-data-sw"},
                        "result": {"ok": True, "detail": "x" * 500},
                    }
                ],
            }
        },
    }
    payload = summary_user_payload(merged)
    assert payload["issues_total"] == 1
    ev = payload["agents"]["bgp"]["evidence"]
    assert len(ev) == 1
    assert ev[0]["check"] == "verify_bgp_peer_reachability"
    assert ev[0]["device"] == "lbnl-data-sw"
    assert "result_preview" in ev[0]
    assert len(ev[0]["result_preview"]) <= 400


def test_compact_evidence_prefers_error():
    rows = _compact_evidence(
        [
            {
                "check": "exec_show",
                "args": {"device_name": "a", "input_command": "isis neighbors"},
                "error": "timeout",
            }
        ]
    )
    assert rows[0]["error"] == "timeout"
    assert "result_preview" not in rows[0]
