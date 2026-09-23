"""Convert operator markdown reports for Slack (mrkdwn) and email (HTML)."""

from __future__ import annotations

import html
import re
from typing import Iterable

from agent.report_format import ReportOutputs

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_UL = re.compile(r"^(\s*)([-*])\s+(.*)$")
_OL = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
_HR = re.compile(r"^(\s*[-*_]\s*){3,}$")
_FENCE = re.compile(r"^```")


def markdown_to_plain(md: str) -> str:
    """Readable plain text: drop heading marks, bold/code fences."""
    lines: list[str] = []
    in_fence = False
    for raw in (md or "").splitlines():
        if _FENCE.match(raw.strip()):
            in_fence = not in_fence
            continue
        if in_fence:
            lines.append(raw)
            continue
        m = _HEADING.match(raw)
        if m:
            lines.append(m.group(2).strip())
            continue
        if _HR.match(raw):
            lines.append("-" * 40)
            continue
        line = raw
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        line = re.sub(r"\*(.+?)\*", r"\1", line)
        lines.append(line)
    return "\n".join(lines).rstrip() + ("\n" if md else "")


def _inline_html(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", escaped)
    return escaped


def _inline_slack(text: str) -> str:
    """Markdown inline → Slack mrkdwn (*bold*, `code`)."""
    # Protect code spans first
    parts: list[str] = []
    last = 0
    for m in re.finditer(r"`([^`]+)`", text):
        parts.append(_slack_boldify(text[last : m.start()]))
        parts.append(f"`{m.group(1)}`")
        last = m.end()
    parts.append(_slack_boldify(text[last:]))
    return "".join(parts)


def _slack_boldify(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    return text


def markdown_to_html(md: str) -> str:
    """Subset markdown → simple HTML document suitable for email clients."""
    body: list[str] = []
    paras: list[str] = []
    list_items: list[str] = []
    list_tag: str | None = None
    in_fence = False
    fence_lines: list[str] = []

    def flush_para() -> None:
        nonlocal paras
        if paras:
            body.append("<p>" + "<br>\n".join(_inline_html(p) for p in paras) + "</p>")
            paras = []

    def flush_list() -> None:
        nonlocal list_items, list_tag
        if list_tag and list_items:
            body.append(f"<{list_tag}>")
            for item in list_items:
                body.append(f"<li>{_inline_html(item)}</li>")
            body.append(f"</{list_tag}>")
        list_items = []
        list_tag = None

    def flush_fence() -> None:
        nonlocal fence_lines
        if fence_lines:
            body.append(
                "<pre><code>"
                + html.escape("\n".join(fence_lines))
                + "</code></pre>"
            )
            fence_lines = []

    for raw in (md or "").splitlines():
        stripped = raw.strip()
        if _FENCE.match(stripped):
            if in_fence:
                flush_fence()
                in_fence = False
            else:
                flush_para()
                flush_list()
                in_fence = True
            continue
        if in_fence:
            fence_lines.append(raw)
            continue

        if not stripped:
            flush_para()
            flush_list()
            continue

        if _HR.match(stripped):
            flush_para()
            flush_list()
            body.append("<hr>")
            continue

        hm = _HEADING.match(raw)
        if hm:
            flush_para()
            flush_list()
            level = min(len(hm.group(1)), 6)
            body.append(f"<h{level}>{_inline_html(hm.group(2).strip())}</h{level}>")
            continue

        um = _UL.match(raw)
        if um:
            flush_para()
            if list_tag != "ul":
                flush_list()
                list_tag = "ul"
            list_items.append(um.group(3))
            continue

        om = _OL.match(raw)
        if om:
            flush_para()
            if list_tag != "ol":
                flush_list()
                list_tag = "ol"
            list_items.append(om.group(3))
            continue

        flush_list()
        paras.append(stripped)

    flush_para()
    flush_list()
    flush_fence()

    style = (
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,"
        "sans-serif;font-size:14px;line-height:1.45;color:#222;}"
        "h1{font-size:1.4em;}h2{font-size:1.2em;margin-top:1.2em;}"
        "h3{font-size:1.05em;margin-top:1em;}code{font-family:ui-monospace,monospace;"
        "background:#f4f4f4;padding:0 3px;border-radius:3px;}"
        "pre{background:#f4f4f4;padding:10px;overflow:auto;}"
        "li{margin:0.25em 0;}"
    )
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<style>{style}</style></head><body>\n"
        + "\n".join(body)
        + "\n</body></html>\n"
    )


def markdown_to_slack(md: str) -> str:
    """Subset markdown → Slack mrkdwn (incoming webhooks / chat.postMessage)."""
    out: list[str] = []
    in_fence = False
    fence_lines: list[str] = []

    def flush_fence() -> None:
        nonlocal fence_lines
        if fence_lines:
            out.append("```")
            out.extend(fence_lines)
            out.append("```")
            fence_lines = []

    for raw in (md or "").splitlines():
        stripped = raw.strip()
        if _FENCE.match(stripped):
            if in_fence:
                flush_fence()
                in_fence = False
            else:
                in_fence = True
            continue
        if in_fence:
            fence_lines.append(raw)
            continue

        if not stripped:
            out.append("")
            continue

        if _HR.match(stripped):
            out.append("────────")
            continue

        hm = _HEADING.match(raw)
        if hm:
            level = len(hm.group(1))
            title = _inline_slack(hm.group(2).strip())
            if level <= 1:
                out.append(f"*{title}*")
            elif level == 2:
                out.append(f"*{title}*")
            else:
                out.append(f"*{title}*")
            continue

        um = _UL.match(raw)
        if um:
            out.append(f"• {_inline_slack(um.group(3))}")
            continue

        om = _OL.match(raw)
        if om:
            out.append(f"{om.group(2)}. {_inline_slack(om.group(3))}")
            continue

        out.append(_inline_slack(stripped))

    flush_fence()
    return "\n".join(out).rstrip() + ("\n" if md else "")


def _chunk_text(text: str, limit: int = 2900) -> Iterable[str]:
    """Split long Slack payloads under section block limits."""
    if len(text) <= limit:
        yield text
        return
    buf: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit and buf:
            yield "".join(buf).rstrip()
            buf = []
            size = 0
        if len(line) > limit:
            # Hard-split an oversized line
            for i in range(0, len(line), limit):
                yield line[i : i + limit]
            continue
        buf.append(line)
        size += len(line)
    if buf:
        yield "".join(buf).rstrip()


def slack_payload_from_markdown(md: str) -> dict:
    """Incoming-webhook JSON body with mrkdwn section blocks."""
    mrkdwn = markdown_to_slack(md)
    chunks = list(_chunk_text(mrkdwn))
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
        for chunk in chunks[:48]  # leave headroom under Slack's 50-block cap
    ]
    if len(chunks) > 48:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "_…truncated — full report is in the run artifact / email._"
                    ),
                },
            }
        )
    # Fallback text for notifications / clients that ignore blocks
    fallback = chunks[0] if chunks else "NSO diagnostic report"
    return {"text": fallback[:3000], "blocks": blocks}


def outputs_from_markdown(md: str) -> ReportOutputs:
    """Build channel-tuned ReportOutputs from a markdown report string."""
    text = md or ""
    return ReportOutputs(
        markdown=text,
        plain=markdown_to_plain(text),
        html=markdown_to_html(text),
    )
