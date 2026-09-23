---
description: Run full multi-agent report (ISIS+BGP+fleet); optional publish
argument-hint: "[publish] [device1,device2]"
---

Run the multi-agent investigation workflow and summarize the report.

1. Parse `$ARGUMENTS` (whitespace-separated):
   - If a token is exactly `publish` → deliver with `--publish` (Slack/email +
     `state/multi_agent/`). Otherwise stay dry-run.
   - Any other token that looks like a device list (`name` or `a,b,c`) → pass as
     `--devices …`. If none, omit `--devices` (default: devices on issues).
2. Confirm repo root + `.env` exist; activate `.venv` if present; then
   `set -a && source .env && set +a`.
3. Run one of:

```bash
# default dry-run
nso-multi-agent-run --spine-only --skip-metrics

# with optional devices (replace DEVICES)
nso-multi-agent-run --spine-only --skip-metrics --devices DEVICES

# only if user passed publish
nso-multi-agent-run --publish --skip-metrics
# (and --devices DEVICES if provided)
```

Prefer `--spine-only` so LLM plan/summary is skipped unless the user
explicitly asks for an LLM summary in chat after the run.

4. Summarize from stdout (and, if published, `state/multi_agent/runs/<run_id>/report.md`
   when the CLI prints that path):
   - Overall / fleet highlights
   - IS-IS issue count + top issues
   - BGP issue count + top issues
   - Device sections if present
   - Action items if present
   - Whether this was dry-run or publish, and artifact paths if any

5. Ground every claim in the run output. Do not invent data.
6. If dry-run, remind that nothing was written under `state/multi_agent/` and
   that `/nso-multi-agent publish` delivers.
