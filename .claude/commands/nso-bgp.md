---
description: Run bidirectional BGP check via multi-agent and summarize issues
argument-hint: "[device-substring]"
---

Run the multi-agent BGP spine and report on it. Do **not** publish.

1. Confirm you are in the NSO_summary_agent repo root and `.env` exists. If not,
   tell the user to copy `.env.example` → `.env` and fill NSO/MCP settings, then stop.
2. Activate `.venv` if present: `source .venv/bin/activate`.
3. Load env: `set -a && source .env && set +a`.
4. Run (dry-run / no Slack / no state write):

```bash
nso-multi-agent-run --bgp-only --spine-only --skip-devices --skip-fleet-spine --skip-metrics
```

5. From the command stdout, summarize the **BGP** section. If `$ARGUMENTS` is non-empty,
   highlight issues/sessions whose device or message matches that substring.
6. Produce a short Markdown summary:
   - Bidirectional totals (total / up / down / degraded / unknown) if present
   - Bulleted issues: severity, peers/devices, message (quote from output)
   - If no issues: say so explicitly
   - End with: dry-run (no `state/multi_agent/` write); suggest `/nso-multi-agent`
     for a full report or `/nso-isis` for IS-IS-only focus

Ground every claim in the CLI output. Do not invent sessions or statuses.
Do not pass `--publish`.
