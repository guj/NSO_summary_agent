"""Generic dataplane investigation telemetry and verification gaps."""
from __future__ import annotations

import time
from typing import Any

BLOCKERS = (
    "query_error", "identity_unverified", "missing_forwarding_evidence",
    "endpoint_access_unavailable", "llm_timeout", "provider_failure",
    "tool_limit", "round_limit", "no_conclusion", "gate_rejected",
    "insufficient_evidence",
)
GAP_SCHEMA = {
    "type": "object",
    "description": "For Unknown, specify the missing PE-readiness evidence and next check. Customer-host tests are outside this agent.",
    "properties": {
        "blocker": {"type": "string", "enum": list(BLOCKERS)},
        "missing_check": {"type": "string"},
        "direction": {"type": "string"},
        "next_check": {"type": "string"},
        "required_access": {"type": "string"},
    },
    "required": ["blocker", "missing_check", "next_check"],
}


def normalize_gap(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    if value.get("blocker") not in BLOCKERS:
        return None
    fields = ("blocker", "missing_check", "direction", "next_check", "required_access")
    out = {k: v.strip() for k in fields
           if isinstance((v := value.get(k)), str) and v.strip()}
    if not all(out.get(k) for k in GAP_SCHEMA["required"]):
        return None
    return {**out, "source": "llm"}


class Investigation:
    def __init__(self):
        self.started = time.monotonic()
        self.llm_seconds = 0.0
        self.mcp_seconds = 0.0
        self.llm_requests = 0
        self.mcp_calls = 0
        self.query_errors = 0
        self.rounds = 0
        self.round_limit = 0
        self.stop_reason = "no_conclusion"

    def llm(self, create, **kwargs):
        started = time.monotonic()
        self.llm_requests += 1
        try:
            result = create(**kwargs)
            self.stop_reason = "no_conclusion"
            return result
        except Exception as exc:
            self.stop_reason = (
                "llm_timeout" if isinstance(exc, TimeoutError)
                or "timeout" in type(exc).__name__.lower()
                else "provider_failure"
            )
            raise
        finally:
            self.llm_seconds += time.monotonic() - started

    async def mcp(self, call, *args, **kwargs):
        started = time.monotonic()
        self.mcp_calls += 1
        try:
            return await call(*args, **kwargs)
        finally:
            self.mcp_seconds += time.monotonic() - started

    def concluded(self, finding):
        if str(finding.get("cause") or "").startswith("[gate]"):
            self.stop_reason = "gate_rejected"
        elif finding.get("dataplane_status") == "unknown":
            self.stop_reason = "concluded_insufficient_evidence"
        else:
            self.stop_reason = "concluded"

    def finish(self, tools_used, tool_limit):
        total = time.monotonic() - self.started
        return {
            "duration_seconds": round(total, 3),
            "llm_seconds": round(self.llm_seconds, 3),
            "mcp_seconds": round(self.mcp_seconds, 3),
            "other_seconds": round(max(0.0, total-self.llm_seconds-self.mcp_seconds), 3),
            "llm_requests": self.llm_requests,
            "mcp_calls": self.mcp_calls,
            "query_errors": self.query_errors,
            "tools_used": tools_used, "tools_allowed": tool_limit,
            "tools_remaining": max(0, tool_limit-tools_used),
            "rounds_used": self.rounds, "rounds_allowed": self.round_limit,
            "stop_reason": self.stop_reason,
        }


def fallback_gap(metrics):
    stop = metrics["stop_reason"]
    blocker = stop if stop in BLOCKERS else "insufficient_evidence"
    next_check = {
        "llm_timeout": "Review the timed-out LLM request and retry a scoped investigation; unused tool budget is not the cause.",
        "provider_failure": "Restore provider availability or allowance before retrying the scoped investigation.",
        "tool_limit": "Review partial evidence and select the next discriminating check.",
        "round_limit": "Review investigation progress and the remaining unanswered check.",
        "gate_rejected": "Collect the evidence required by the rejected conclusion before reassessing.",
    }.get(blocker, "Identify the missing PE-readiness evidence in the observations before choosing a targeted follow-up.")
    return {
        "blocker": blocker,
        "missing_check": "PE-readiness conclusion incomplete; see recorded observations and cause.",
        "next_check": next_check,
        "source": "runner",
    }
