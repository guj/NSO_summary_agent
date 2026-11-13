"""How a scan's LLM work went: the Result's "LLM investigations" line.

A provider's error is kept as its category (``agent.summarize.llm_error_category``), never as its
text: the text can hold a model server's address, and the report goes to Slack and email.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from diagnostic_mas.case import CaseFile

# Why an investigation ended without a conclusion.
PROVIDER_ERROR, REFUSED, NO_CONCLUSION = "provider_error", "refused", "no_conclusion"
# How the summary request ended (``case.llm_status["summary"]``).
WRITTEN, FAILED, EMPTY, NOT_REQUESTED = "written", "failed", "empty", "not_requested"

_WORD_BREAK = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def error_category(exc: BaseException) -> str:
    """A failed LLM request's category; the one thing about the error that is kept."""
    from agent.summarize import llm_error_category

    return llm_error_category(exc)


def error_phrase(category: Any) -> str:
    """A provider error's category in words: "provider connection error", "provider timed out"."""
    category = str(category or "").strip()
    if category == "connection_error":
        return "provider connection error"
    if category == "connection_timeout":
        return "provider connection timed out"
    if "timeout" in category.lower():
        return "provider timed out"
    words = _WORD_BREAK.sub(" ", category).replace("_", " ").lower().removeprefix("api ")
    return f"provider {words}" if words else "provider error"


def note_stop(case: CaseFile, kind: str, subject: str, why: str, error: str | None = None) -> None:
    """Record why a routing investigation ended without a conclusion."""
    case.llm_status.setdefault("stops", []).append(
        {"kind": kind, "subject": subject, "why": why} | ({"error": error} if error else {}))


def note_summary_failed(case: CaseFile, category: str) -> None:
    case.llm_status.update({"summary": FAILED, "summary_error": category})


def stop_phrase(why: Any, error: Any = None) -> str:
    """Why one investigation did not finish, for its own report line."""
    if why == PROVIDER_ERROR:
        return f"the LLM request failed ({error_phrase(error)})"
    if why == REFUSED:
        return "its conclusion was refused repeatedly"
    return "it ended without a conclusion"


def _service_outcomes(case: CaseFile) -> list[tuple[bool, str, str | None]]:
    """(reached a conclusion, why not, provider error) for each service the LLM investigated."""
    from diagnostic_mas.port_investigation import SOURCES

    out = []
    for dx in case.diagnoses:
        if dx.get("kind") != "dataplane" or dx.get("source") in SOURCES:
            continue
        metrics = dx.get("investigation") if isinstance(dx.get("investigation"), dict) else {}
        stop = str(metrics.get("stop_reason") or "")
        if stop.startswith("concluded") or stop == "gate_rejected" or (not stop and dx.get("complete") is not False):
            out.append((True, "", None))
        elif stop in ("provider_failure", "llm_timeout"):
            out.append((False, PROVIDER_ERROR, metrics.get("llm_error") or ("timeout" if stop == "llm_timeout" else "")))
        else:
            out.append((False, REFUSED if metrics.get("refused_conclusions") else NO_CONCLUSION, None))
    return out


def _outcomes(case: CaseFile) -> list[tuple[bool, str, str | None]]:
    """Every investigation the LLM started this scan: services, ports and devices, routing."""
    from diagnostic_mas.port_investigation import SOURCES

    out = _service_outcomes(case)
    for ev in case.evidence:
        payload = ev.get("payload") if ev.get("kind") in SOURCES else None
        if isinstance(payload, dict):
            out.append((bool(payload.get("concluded")), str(payload.get("stopped") or NO_CONCLUSION),
                        payload.get("stopped_error")))
    stops = [s for s in case.llm_status.get("stops") or [] if s.get("kind") == "routing"]
    started = max(int(getattr(case.budget, "drill_issues_used", 0) or 0), len(stops))
    out.extend((True, "", None) for _ in range(started - len(stops)))
    out.extend((False, str(s.get("why") or NO_CONCLUSION), s.get("error")) for s in stops)
    return out


def _unfinished(outcomes: list[tuple[bool, str, str | None]]) -> str:
    """ "2 failed: provider connection error; 1 stopped after refused conclusions" """
    failed = Counter(error_phrase(error) for done, why, error in outcomes if not done and why == PROVIDER_ERROR)
    refused = sum(1 for done, why, _ in outcomes if not done and why == REFUSED)
    other = sum(1 for done, why, _ in outcomes if not done and why not in (PROVIDER_ERROR, REFUSED))
    bits = [f"{n} failed: {phrase}" for phrase, n in failed.items()]
    if refused:
        bits.append(f"{refused} stopped after refused conclusions")
    if other:
        bits.append(f"{other} ended without a conclusion")
    return "; ".join(bits)


def llm_outcome(case: CaseFile) -> dict[str, Any] | None:
    """How the scan's LLM work went: its state and how many investigations reached a conclusion.

    The state is complete, partial, unavailable, skipped or halted; None for a case saved before this
    was recorded. The Result line and the pushed metrics both come from this.
    """
    status = getattr(case, "llm_status", None) or {}
    halted = bool(getattr(case, "llm_halt_reason", None) or status.get("halted"))
    if not status and not halted:
        return None
    if status.get("skipped"):
        return {"state": "skipped", "done": 0, "total": 0}
    outcomes = _outcomes(case)
    total, done = len(outcomes), sum(1 for reached, _, _ in outcomes if reached)
    counts = {"done": done, "total": total}
    if halted:
        return {"state": "halted", **counts}
    summary = status.get("summary")
    provider_failed = [error for reached, why, error in outcomes if not reached and why == PROVIDER_ERROR]
    if summary == FAILED and not done and len(provider_failed) == total:
        return {"state": "unavailable", **counts}
    complete = done == total and summary in (WRITTEN, NOT_REQUESTED)
    return {"state": "complete" if complete else "partial", **counts}


def llm_result_text(case: CaseFile) -> str | None:
    """The Result's "LLM investigations" text; None for a case saved before this was recorded."""
    outcome = llm_outcome(case)
    if outcome is None:
        return None
    status = case.llm_status
    if outcome["state"] == "skipped":
        return f"Skipped — {status['skipped']}."
    outcomes = _outcomes(case)
    total, done = outcome["total"], outcome["done"]
    counted = (f"{done} of {total} investigation{'s' if total != 1 else ''} reached a conclusion"
               if total else "no investigation was needed")
    stands = " Fixed-check results stand."
    if outcome["state"] == "halted":
        return f"Halted — the provider's spend limit was reached; {counted}; no LLM summary.{stands}"
    summary = status.get("summary")
    if outcome["state"] == "unavailable":
        return (f"Unavailable — no LLM request succeeded ({error_phrase(status.get('summary_error'))})."
                f"{stands}")
    summary_text = {
        WRITTEN: "summary written",
        NOT_REQUESTED: "LLM summary not requested",
        EMPTY: "no LLM summary (empty reply)",
        FAILED: f"no LLM summary ({error_phrase(status.get('summary_error'))})",
    }.get(summary, "LLM summary not recorded")
    provider_failed = [error for reached, why, error in outcomes if not reached and why == PROVIDER_ERROR]
    unfinished = _unfinished(outcomes)
    return (f"{'Complete' if outcome['state'] == 'complete' else 'Partial'} — {counted}"
            + (f" ({unfinished})" if unfinished else "")
            + f"; {summary_text}."
            + (stands if provider_failed or summary == FAILED else ""))
