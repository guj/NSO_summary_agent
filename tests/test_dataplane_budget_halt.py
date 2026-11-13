"""Integration: dataplane phase stops on provider budget_exceeded."""

from __future__ import annotations

import pytest

from diagnostic_mas.case import Budget, CaseFile, add_evidence
from diagnostic_mas.dataplane_verify import run_dataplane_verify_phase


class _BudgetBoom:
    def __init__(self):
        self.n = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):  # type: ignore[override]
        return self

    def create(self, **_kwargs):
        self.n += 1
        raise RuntimeError(
            "Error code: 429 - {'error': {'message': "
            "'ExceededBudget: User=x over budget. Spend=50.1, Budget=50.0', "
            "'type': 'budget_exceeded', 'code': '429'}}"
        )


@pytest.mark.asyncio
async def test_dataplane_phase_halts_on_provider_budget(monkeypatch):
    case = CaseFile(
        budget=Budget(
            max_deep_checks=0,
            max_handoffs=0,
            max_dataplane_tools=5,
        )
    )
    services = {}
    for stype in ("l2ptp", "l2sts", "l3rt"):
        for i in range(2):
            name = f"{stype}-{i}"
            services[f"{stype}/{name}"] = {
                "name": name,
                "service_type": stype,
                "system_status": "up",
                "dataplane_status": "not_checked",
                "status": "up",
                "devices": ["a-sw", "b-sw"],
            }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {"extra": {"services": services}},
        },
    )
    boom = _BudgetBoom()
    await run_dataplane_verify_phase(
        client=None,
        settings=type(
            "S",
            (),
            {
                "fabric_model": "m",
                "fabric_api_key": "sk",
                "fabric_api_url": "https://example",
            },
        )(),
        case=case,
        device_names={"a-sw", "b-sw"},
        max_per_category=2,
        openai_client=boom,
        category_rotate_seed="halt-test",
    )
    assert case.llm_halt_reason
    assert "budget_exceeded" in case.llm_halt_reason
    # Only one dig attempted (first LLM call failed); remainder marked.
    assert boom.n == 1
    halted = [
        n
        for n, cov in case.service_coverage.items()
        if cov == "llm_budget_exceeded"
    ]
    assert len(halted) >= 4
    # The provider's own message (it can name a user or an address) stays out of what the report shows.
    shown = " ".join(f"{dx.get('observed')} {dx.get('cause')}" for dx in case.diagnoses)
    assert "the provider's LLM spend limit was reached" in shown
    assert "User=x" not in shown and "Spend=" not in shown


@pytest.mark.asyncio
async def test_the_dataplane_step_fills_its_cap_with_services_that_are_still_listed(monkeypatch, capsys):
    """test68: six of ten chosen l2bridge services were deleted by their turn and their slots stayed empty."""
    from unittest.mock import AsyncMock

    from diagnostic_mas import service_presence as sp
    import diagnostic_mas.dataplane_verify as dv

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0, max_dataplane_tools=5))
    services = {f"l2bridge/link-{i}": {"name": f"link-{i}", "service_type": "l2bridge", "system_status": "up",
                                       "dataplane_status": "not_checked", "status": "up", "devices": ["a-sw"]}
                for i in range(6)}
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services",
                        "payload": {"extra": {"services": services}}})
    monkeypatch.setattr(sp, "call_mcp", AsyncMock(return_value={"status": "success", "data": {"services": [
        {"name": "link-3"}, {"name": "link-4"}, {"name": "link-5"}]}}))
    started = []

    async def verify(client, settings, case, *, record, **_kw):
        started.append(record["name"])
        return True

    monkeypatch.setattr(dv, "llm_dataplane_verify_one", verify)
    await run_dataplane_verify_phase(
        client=None, settings=type("S", (), {"fabric_model": "m", "fabric_api_key": "k", "fabric_api_url": "u"})(),
        case=case, device_names={"a-sw"}, max_services=2, max_per_category=30, openai_client=object())

    assert sorted(started) == ["link-3", "link-4"]                 # two still listed, so the cap of 2 is met
    gone = {n for n, cov in case.service_coverage.items() if cov == "disappeared"}
    assert gone <= {"link-0", "link-1", "link-2"} and {"link-0", "link-1"} <= gone
    assert "candidates=2" in capsys.readouterr().err
