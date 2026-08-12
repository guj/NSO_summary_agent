"""Tests for delta logic (no live NSO required)."""

from agent.delta import compute_delta


def test_delta_first_run():
    delta = compute_delta({"counts": {}}, None)
    assert delta["first_run"] is True


def test_delta_count_change():
    prev = {"counts": {"l2vpn": {"total": 10, "up": 10, "down": 0, "degraded": 0, "unknown": 0}}}
    cur = {"counts": {"l2vpn": {"total": 10, "up": 9, "down": 1, "degraded": 0, "unknown": 0}}}
    delta = compute_delta(cur, prev)
    assert delta["counts"]["l2vpn"]["down"] == 1
