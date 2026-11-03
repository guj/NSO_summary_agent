"""Report text per delivery channel."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReportOutputs:
    """Report text tuned per channel (markdown tables do not render in Slack/email)."""

    markdown: str
    plain: str
    html: str
