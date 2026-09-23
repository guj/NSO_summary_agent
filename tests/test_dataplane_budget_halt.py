"""Integration: dataplane phase stops on provider budget_exceeded."""

from __future__ import annotations

import pytest

from diagnostic_mas.case import Budget, CaseFile, add_evidence
from diagnostic_mas.dataplane_verify import run_dataplane_verify_phase


class _BudgetBoom:
    def __init__(self):
        self.n = 0

    class completions:
        pass

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
