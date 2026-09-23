---
description: Service-focused diagnostic; filter by type, id, and/or device
argument-hint: "[llm] [l3rt|type=…] [id=…] [device…]"
---

Run a **service-first** diagnostic MAS pass. Do **not** publish.

1. Parse `$ARGUMENTS` (whitespace-separated). Recognized forms (case-insensitive):
   - `llm` → enable LLM (omit `--skip-llm`). Default: keep `--skip-llm`.
   - `type=TYPE` / `service-type=TYPE` / `type:TYPE` → `--service-type TYPE`
   - `id=NAME` / `service-id=NAME` / `id:NAME` → `--service-id NAME`
   - **Bare service-type token** (no `=` / `:`): if the token looks like a service
     type (e.g. `l3rt`, `l2ptp`, `l3vpn`, or contains no `-data-` / `-sw` device
     pattern), treat it as `--service-type TOKEN`.
     Examples: `/nso-service l3rt` → `--service-type l3rt`
   - Device tokens: names with `-data-`, ending in `-sw`/`-rr`, comma-lists, or
     explicit `device=NAME` → `--devices …`
   - Type + id together = intersection. If nothing matches, CLI exits without a
     broad scan.
2. Confirm repo root + `.env`; activate `.venv` if present; then
   `set -a && source .env && set +a`.
3. Run (dry-run). `--service-type` / `--service-id` **imply lean service-only**
   (no need to pass `--service-only`):

```bash
# /nso-service l3rt
nso-diagnostic-run --skip-llm --dry-run --service-type l3rt

# /nso-service type=l2ptp id=fabric-l2ptp-t1
nso-diagnostic-run --skip-llm --dry-run --service-type l2ptp --service-id fabric-l2ptp-t1

# /nso-service l2ptp  (with llm: basic checks all; LLM only on suspicious)
nso-diagnostic-run --dry-run --service-type l2ptp --max-drill-issues 2
```

   **Service-first flow:**
   1. Find matching instances (+ endpoint devices)
   2. Deterministic sync / live L2 / shared device probes
   3. Prioritize suspicious for dataplane LLM (budget: `--max-drill-issues` count,
      `--max-dataplane-tools` per service); optional drill; expand ISIS/BGP on
      endpoints only when evidence calls for it
   4. Report every match with Coverage: basic passed / investigated /
      unresolved / budget-skipped

4. Summarize from stdout (operator report):
   - Header Result + Services (every match + Coverage)
   - Devices (endpoint Attention)
   - Recommended follow-up
   - If nothing matched: say so explicitly (CLI already exits)
5. Ground every claim in CLI output. No inventing. No `--publish` / `DRY_RUN=0`.
6. End noting dry-run + LLM on/off; suggest `/nso-device`, `/nso-report`,
   `/nso-isis`, `/nso-bgp`.
