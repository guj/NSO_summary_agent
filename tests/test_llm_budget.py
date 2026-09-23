"""Tests for provider LLM spend-budget detection and halt helpers."""

from __future__ import annotations

from agent.llm_budget import (
    ProviderBudgetExceeded,
    case_llm_halted,
    is_provider_budget_exceeded,
    mark_case_llm_halt,
    provider_budget_message,
)
from diagnostic_mas.case import Budget, CaseFile


def test_is_provider_budget_exceeded_nso19_shape():
    exc = Exception(
        "Error code: 429 - {'error': {'message': 'ExceededBudget: "
        "User=jgu@lbl.gov over budget. Spend=50.007, Budget=50.0', "
        "'type': 'budget_exceeded', 'param': None, 'code': '429'}}"
    )
    assert is_provider_budget_exceeded(exc)
    assert "ExceededBudget" in provider_budget_message(exc) or "budget" in (
        provider_budget_message(exc).lower()
    )


def test_is_provider_budget_exceeded_ignores_plain_errors():
    assert not is_provider_budget_exceeded(TimeoutError("timed out"))
    assert not is_provider_budget_exceeded(RuntimeError("rate limit rpm"))


def test_mark_case_llm_halt_idempotent():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    mark_case_llm_halt(case, "first")
    mark_case_llm_halt(case, "second")
    assert case.llm_halt_reason == "first"
    assert case_llm_halted(case)
    raised = ProviderBudgetExceeded("boom")
    assert str(raised) == "boom"
