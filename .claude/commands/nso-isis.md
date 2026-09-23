---
description: Run IS-IS-focused diagnostic (nso-diagnostic-run) and summarize issues
argument-hint: "[llm] [device|device1,device2]"
---

Run the diagnostic MAS **IS-IS** spine and report on it. Do **not** publish.

1. Parse `$ARGUMENTS` (whitespace-separated):
   - Token `llm` (case-insensitive) → enable LLM (omit `--skip-llm`). Default: `--skip-llm`.
   - Any other token → device filter. Prefer one comma-list token
     (`renc-data-sw,lbnl-data-sw`) or a substring (`renc`).
     Pass to CLI as `--devices …`. If none, omit `--devices` (all devices).
2. Confirm repo root + `.env`; activate `.venv` if present; then
   `set -a && source .env && set +a`.
3. Run (dry-run / no Slack / no state write):

```bash
# default — all devices, spines only
nso-diagnostic-run --isis-only --skip-service --skip-llm --dry-run

# one device / substring
nso-diagnostic-run --isis-only --skip-service --skip-llm --dry-run --devices renc

# with llm (drop --skip-llm)
nso-diagnostic-run --isis-only --skip-service --dry-run [--devices DEVICES]
```

   Notes:
   - `--isis-only` skips BGP (physical inventory still runs for the device set).
   - `--skip-service` skips service/fleet/HW.
   - `--devices` focuses on seed device(s); the CLI expands to **one-hop IS-IS
     neighbors only** (not the whole inventory), then reports on the seed.
   - Default `--skip-llm` = spines + report only; `llm` enables diagnosis if
     `FABRIC_AI_*` is set.

4. Summarize **IS-IS / underlay** from stdout (focus on the filtered device if any).
5. Short Markdown: bidirectional totals; bulleted issues (quote); remedies if `llm`;
   say explicitly if no issues. End: dry-run; LLM on/off; suggest `/nso-bgp`,
   `/nso-report`, `/nso-device`.

Ground every claim in CLI output. No inventing. No `--publish` / `DRY_RUN=0`.
