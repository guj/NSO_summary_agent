---
description: Run bidirectional IS-IS check via multi-agent and summarize issues
argument-hint: "[device-substring]"
---

Run the multi-agent IS-IS spine and report on it. Do **not** publish.

1. Confirm you are in the NSO_summary_agent repo root and `.env` exists. If not,
   tell the user to copy `.env.example` → `.env` and fill NSO/MCP settings, then stop.
2. Activate `.venv` if present: `source .venv/bin/activate`.
3. Load env: `set -a && source .env && set +a`.
4. Run (dry-run / no Slack / no state write):

```bash
nso-multi-agent-run --isis-only --spine-only --skip-devices --skip-fleet-spine --skip-metrics
```

5. From the command stdout, summarize the **IS-IS** section. If `$ARGUMENTS` is non-empty,
   highlight issues/adjacencies whose device or message matches that substring.
6. Produce a short Markdown summary:
   - Bidirectional totals (total / up / down / unidirectional / unknown) if present
   - Bulleted issues: severity, edge/devices, message (quote from output)
   - If no issues: say so explicitly
   - End with: dry-run (no `state/multi_agent/` write); suggest `/nso-multi-agent`
     for a full report or `/nso-bgp` for BGP-only focus

Ground every claim in the CLI output. Do not invent adjacencies or statuses.
Do not pass `--publish`.
