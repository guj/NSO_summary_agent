"""What a scan used of the LLM: token counts from any endpoint, spend and budget where the provider reports them."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent import llm_usage
from agent.llm_usage import (
    LlmSpend, begin_llm_accounting, budget_line, count_usage, end_llm_accounting, llm_usage_totals,
    read_llm_spend, record_llm_usage, reset_llm_usage, usage_lines, usage_record,
)
from test_fabric_key import _settings

ROOT = "https://llm.example"
# The numbers the proxy gave on 13 October 2026: the key has a running total and no budget; its user has a monthly one.
KEY_INFO = {"key": "hashed", "info": {"spend": 444.04702, "max_budget": None, "budget_reset_at": None}}
USER_INFO = {"user_id": "u", "user_info": {"spend": 280.53905, "max_budget": 500.0, "budget_duration": "1mo",
                                            "budget_reset_at": "2026-11-01T00:00:00Z"}}


def _answer(prompt: int, completion: int):
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion))


def _provider(answers: dict, asked: list | None = None):
    def get(url: str, key: str):
        assert key == "sk-test"
        if asked is not None:
            asked.append(url)
        return answers.get(url)
    return get


# --- token counts: any OpenAI-compatible endpoint -----------------------------------

def test_token_counts_add_up_and_an_answer_without_them_is_still_a_request():
    reset_llm_usage()
    record_llm_usage(_answer(1200, 80))
    record_llm_usage({"usage": {"prompt_tokens": 300, "completion_tokens": 20}})
    record_llm_usage(SimpleNamespace(usage=None))

    assert llm_usage_totals() == {"requests": 3, "input_tokens": 1500, "output_tokens": 100, "uncounted": 1}
    reset_llm_usage()
    assert llm_usage_totals()["requests"] == 0


def test_client_counts_every_answer_and_hands_it_back_unchanged():
    answer = _answer(10, 2)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if kwargs.get("fail"):
            raise RuntimeError("no answer")
        return answer

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    reset_llm_usage()
    assert count_usage(client) is client

    assert client.chat.completions.create(model="m") is answer
    with pytest.raises(RuntimeError):
        client.chat.completions.create(fail=True)

    assert calls == [{"model": "m"}, {"fail": True}]
    assert llm_usage_totals() == {"requests": 1, "input_tokens": 10, "output_tokens": 2, "uncounted": 0}


def test_every_client_the_program_builds_counts_its_answers():
    from agent.summarize import fabric_openai_client

    client = fabric_openai_client(_settings(fabric_api_url=f"{ROOT}/v1"))

    assert getattr(client.chat.completions.create, "counts_usage", False) is True


def test_model_probe_at_start_up_is_counted_too():
    import json
    from unittest.mock import patch
    from agent.fabric_key import resolve_fabric_model

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"model": "m", "usage": {"prompt_tokens": 8, "completion_tokens": 1}}).encode()

    reset_llm_usage()
    with patch("agent.fabric_key.urllib.request.urlopen", return_value=_Response()):
        resolve_fabric_model(_settings())

    assert llm_usage_totals() == {"requests": 1, "input_tokens": 8, "output_tokens": 1, "uncounted": 0}


# --- spend and budget: only where the provider reports them --------------------------

def test_spend_is_read_at_the_provider_root_even_when_the_url_ends_in_v1():
    asked = []
    spend = read_llm_spend(_settings(fabric_api_url=f"{ROOT}/v1/"),
                           get=_provider({f"{ROOT}/key/info": KEY_INFO, f"{ROOT}/user/info": USER_INFO}, asked))

    assert asked == [f"{ROOT}/key/info", f"{ROOT}/user/info"]
    assert spend == LlmSpend(key_spend=444.04702, budget_spend=280.53905, budget_max=500.0, budget_period="1mo",
                             budget_reset_at=datetime(2026, 11, 1, tzinfo=UTC))
    assert round(spend.budget_left, 2) == 219.46


def test_budget_set_on_the_key_itself_is_the_one_shown():
    own = {"info": {"spend": 12.5, "max_budget": 20.0, "budget_duration": "30d", "budget_reset_at": "2026-11-05"}}
    spend = read_llm_spend(_settings(fabric_api_url=ROOT),
                           get=_provider({f"{ROOT}/key/info": own, f"{ROOT}/user/info": USER_INFO}))

    assert (spend.key_spend, spend.budget_spend, spend.budget_max, spend.budget_period) == (12.5, 12.5, 20.0, "30d")


def test_provider_that_reports_nothing_gives_no_spend():
    assert read_llm_spend(_settings(fabric_api_url=f"{ROOT}/v1"), get=_provider({})) is None
    assert read_llm_spend(_settings(fabric_api_url=ROOT), get=_provider({f"{ROOT}/key/info": {"detail": "x"}})) is None


def test_line_at_the_start_says_what_is_left_or_that_the_provider_does_not_say():
    full = LlmSpend(key_spend=444.04702, budget_spend=280.53905, budget_max=500.0, budget_period="1mo",
                    budget_reset_at=datetime(2026, 11, 1, tzinfo=UTC))

    assert budget_line(full) == "LLM budget: $280.54 of $500.00 used (1mo), $219.46 left, resets 2026-11-01."
    assert budget_line(LlmSpend(key_spend=444.04702)) == "LLM spend on this key so far: $444.05. No budget is set."
    assert budget_line(None) == "LLM spend: this provider does not report spend or budget."


# --- the record kept with the scan, and its lines in Run details ----------------------

BEFORE = LlmSpend(key_spend=444.04702, budget_spend=280.53905, budget_max=500.0, budget_period="1mo",
                  budget_reset_at=datetime(2026, 11, 1, tzinfo=UTC))
AFTER = LlmSpend(key_spend=446.28702, budget_spend=282.77905, budget_max=500.0, budget_period="1mo",
                 budget_reset_at=datetime(2026, 11, 1, tzinfo=UTC))
TOTALS = {"requests": 27, "input_tokens": 1234567, "output_tokens": 45678, "uncounted": 0}


def test_scan_cost_is_the_keys_spend_at_the_end_less_its_spend_at_the_start():
    record = usage_record(BEFORE, AFTER, TOTALS)

    assert record == {"requests": 27, "input_tokens": 1234567, "output_tokens": 45678, "uncounted": 0,
                      "cost": 2.24, "spend_before": 444.04702, "spend_after": 446.28702,
                      "budget_spend": 282.77905, "budget_max": 500.0, "budget_left": 217.22095,
                      "budget_period": "1mo", "budget_reset_at": "2026-11-01"}
    # One line for this scan; the budget is shown at the start of the scan, not here (14 Oct 2026).
    assert usage_lines(record) == [
        "**LLM this scan:** $2.24 · 27 requests · 1,234,567 input and 45,678 output tokens. "
        "The last request may not be counted yet.",
    ]


def test_provider_without_spend_still_gets_its_token_counts():
    record = usage_record(None, None, TOTALS | {"uncounted": 2})

    assert record["cost"] is None and "budget_max" not in record
    assert usage_lines(record) == [
        "**LLM this scan:** 27 requests · 1,234,567 input and 45,678 output tokens · "
        "2 answers gave no token counts; no spend reading from the provider.",
    ]
    # A scan that ran without the LLM has no record and adds no lines.
    assert usage_lines({}) == [] and usage_lines(None) == []


def test_a_reading_that_failed_at_the_end_is_not_taken_for_a_provider_without_spend():
    # 14 Oct 2026: the proxy refused connections at the end of the scan, after reporting at the start.
    record = usage_record(BEFORE, None, TOTALS)

    assert record["cost"] is None and record["missing_reading"] == "end"
    assert usage_lines(record) == [
        "**LLM this scan:** 27 requests · 1,234,567 input and 45,678 output tokens; "
        "cost unknown: the provider gave no spend reading at the end of the scan.",
    ]


def test_a_reading_that_failed_at_the_start_is_said_too():
    record = usage_record(None, AFTER, TOTALS)

    assert record["cost"] is None and record["missing_reading"] == "start"
    assert usage_lines(record) == [
        "**LLM this scan:** 27 requests · 1,234,567 input and 45,678 output tokens; "
        "cost unknown: the provider gave no spend reading at the start of the scan.",
    ]


def test_cost_is_not_guessed_when_only_one_reading_exists_or_the_total_went_back():
    assert usage_record(BEFORE, None, TOTALS)["cost"] is None
    assert usage_record(AFTER, BEFORE, TOTALS)["cost"] is None
    # With a budget and no running total on the key, the account's spend is the measure.
    by_account = usage_record(LlmSpend(budget_spend=10.0, budget_max=50.0), LlmSpend(budget_spend=10.5, budget_max=50.0), TOTALS)
    assert by_account["cost"] == 0.5
    assert usage_lines(by_account)[0].startswith("**LLM this scan:** $0.50 (from the account's spend) · 27 requests")


def test_accounting_starts_the_counts_afresh_and_ends_with_the_record():
    settings = _settings(fabric_api_url=f"{ROOT}/v1")
    record_llm_usage(_answer(5, 5))                     # left over from before the scan

    line = begin_llm_accounting(settings, get=_provider({f"{ROOT}/key/info": KEY_INFO, f"{ROOT}/user/info": USER_INFO}))
    record_llm_usage(_answer(1000, 50))
    later_key = {"info": {"spend": 444.54702}}
    record = end_llm_accounting(settings, get=_provider({f"{ROOT}/key/info": later_key, f"{ROOT}/user/info": USER_INFO}))

    assert line == "LLM budget: $280.54 of $500.00 used (1mo), $219.46 left, resets 2026-11-01."
    assert (record["requests"], record["input_tokens"], record["output_tokens"], record["cost"]) == (1, 1000, 50, 0.5)


def test_reading_the_provider_never_stops_a_scan(monkeypatch):
    def broken(*a, **k):
        raise OSError("no route")

    monkeypatch.setattr(llm_usage.urllib.request, "urlopen", broken)

    assert read_llm_spend(_settings(fabric_api_url=f"{ROOT}/v1")) is None


# --- kept with the scan and shown in the report ---------------------------------------

def test_record_is_saved_with_the_scan_and_shown_under_run_details():
    from diagnostic_mas.case import Budget, CaseFile
    from diagnostic_mas.report import render_report
    from diagnostic_mas.state_paths import case_to_dict

    case = CaseFile(budget=Budget(0, 0))
    case.llm_usage = usage_record(BEFORE, AFTER, TOTALS)

    assert case_to_dict(case)["llm_usage"]["cost"] == 2.24
    report = render_report(case, summary="s", duration_seconds=1, dry_run=True, llm_model="m")
    details = report[report.index("## Run details"):]
    assert "**LLM this scan:** $2.24 · 27 requests · 1,234,567 input and 45,678 output tokens." in details
    assert "LLM budget" not in details

    plain = render_report(CaseFile(budget=Budget(0, 0)), summary="s", duration_seconds=1, dry_run=True, llm_model="m")
    assert "LLM this scan" not in plain


async def test_scan_shows_the_budget_at_the_start_and_keeps_what_it_used(tmp_path, capsys):
    import argparse
    import json
    from unittest.mock import AsyncMock, MagicMock, patch

    from diagnostic_mas import run as run_mod
    from test_diagnostic_mas_publish import _settings as scan_settings

    args = argparse.Namespace(
        skip_llm=False, dry_run=False, publish=True, isis_only=False, bgp_only=False, device_only=False,
        service_only=False, skip_service=True, devices="", service_type="", service_id="",
        max_deep_checks=0, max_handoffs=0)

    class _Session:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            return False

    record = usage_record(BEFORE, AFTER, TOTALS)
    with patch.object(run_mod, "load_settings", return_value=scan_settings(state_dir=tmp_path, dry_run=True)), \
         patch.object(run_mod, "prepare_fabric_llm", return_value=(None, False)), \
         patch.object(run_mod, "begin_llm_accounting", return_value="LLM budget: $280.54 of $500.00 used.") as begin, \
         patch.object(run_mod, "end_llm_accounting", return_value=record) as end, \
         patch.object(run_mod, "mcp_session", return_value=_Session()), \
         patch.object(run_mod, "call_mcp", new=AsyncMock(return_value={"status": "success", "data": {"device": []}})), \
         patch.object(run_mod, "parse_device_names", return_value=["renc-data-sw"]), \
         patch.object(run_mod, "run_mandatory_spines", new=AsyncMock()), \
         patch.object(run_mod, "summary_narrative", new=AsyncMock(return_value="summary")), \
         patch.object(run_mod, "publish_all", return_value=["slack"]):
        assert await run_mod._run(args) == 0

    assert begin.call_count == end.call_count == 1
    assert "LLM budget: $280.54 of $500.00 used." in capsys.readouterr().err
    saved = next((tmp_path / "diagnostic_mas" / "runs").iterdir())
    assert json.loads((saved / "case.json").read_text())["llm_usage"]["cost"] == 2.24
    assert "**LLM this scan:** $2.24" in (saved / "report.md").read_text()


async def test_scan_without_the_llm_asks_the_provider_nothing(tmp_path):
    import argparse
    from unittest.mock import AsyncMock, MagicMock, patch

    from diagnostic_mas import run as run_mod
    from test_diagnostic_mas_publish import _settings as scan_settings

    args = argparse.Namespace(
        skip_llm=True, dry_run=False, publish=True, isis_only=False, bgp_only=False, device_only=False,
        service_only=False, skip_service=True, devices="", service_type="", service_id="",
        max_deep_checks=0, max_handoffs=0)

    class _Session:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            return False

    with patch.object(run_mod, "load_settings", return_value=scan_settings(state_dir=tmp_path, dry_run=True)), \
         patch.object(run_mod, "begin_llm_accounting") as begin, \
         patch.object(run_mod, "end_llm_accounting") as end, \
         patch.object(run_mod, "mcp_session", return_value=_Session()), \
         patch.object(run_mod, "call_mcp", new=AsyncMock(return_value={"status": "success", "data": {"device": []}})), \
         patch.object(run_mod, "parse_device_names", return_value=["renc-data-sw"]), \
         patch.object(run_mod, "run_mandatory_spines", new=AsyncMock()), \
         patch.object(run_mod, "summary_narrative", new=AsyncMock(return_value="summary")), \
         patch.object(run_mod, "publish_all", return_value=["slack"]):
        assert await run_mod._run(args) == 0

    assert begin.call_count == end.call_count == 0
    saved = next((tmp_path / "diagnostic_mas" / "runs").iterdir())
    assert "LLM usage" not in (saved / "report.md").read_text()
