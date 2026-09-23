---
description: Full diagnostic report (nso-diagnostic-run); optional llm / publish / devices
argument-hint: "[llm] [publish] [max-deep-checks=N] [max-dataplane-tools=N] [max-dataplane-per-category=N] [max-dataplane-services=N] [max-drill-issues=N] [max-tools-per-drill=N] [device1,device2]"
---

Run the **full** diagnostic MAS report (`nso-diagnostic-run`).

1. Parse `$ARGUMENTS` (whitespace-separated):
   - Token `llm` (case-insensitive) → enable LLM (omit `--skip-llm`). Default: `--skip-llm`.
   - Token `publish` (case-insensitive) → deliver with `--publish` (Slack/email +
     `state/diagnostic_mas/`). Otherwise stay dry-run (`--dry-run`).
   - Token `max-deep-checks=N` → `--max-deep-checks N` (default **0** = skip
     autonomous planner; Fabric plan turns are multi-minute — only opt in when needed).
   - Token `max-dataplane-tools=N` → `--max-dataplane-tools N` (default **40**
     MCP calls per service dataplane LLM verify).
   - Token `max-dataplane-per-category=N` → `--max-dataplane-per-category N`
     (up to N digs per typed prompt category; e.g. overnight sample of 10).
   - Token `max-dataplane-services=N` → `--max-dataplane-services N` (optional
     total dig cap after per-category selection).
   - Token `max-drill-issues=N` → `--max-drill-issues N` (default **2** Issues to drill).
   - Token `max-tools-per-drill=N` or legacy `max-drills=N` → `--max-tools-per-drill N`
     (default **12** MCP tool calls **per** drilled Issue).
   - Any other token → `--devices …` (comma-list or substring). If none, all devices.
2. Confirm repo root + `.env`; activate `.venv` if present; then
   `set -a && source .env && set +a`.
3. Run:

```bash
# default — spines only, dry-run
nso-diagnostic-run --skip-llm --dry-run

# with devices
nso-diagnostic-run --skip-llm --dry-run --devices DEVICES

# with llm (drop --skip-llm): dataplane + drill; autonomous OFF by default
# Service dataplane verify runs first; those services skip drill re-work.
nso-diagnostic-run --dry-run --max-drill-issues 2 --max-tools-per-drill 12 [--devices DEVICES]

# overnight even sample (up to N digs per typed category: l2ptp/l2sts/l3rt)
nso-diagnostic-run --dry-run --max-dataplane-per-category 10

# opt-in slow autonomous planner (Fabric ~45s timeout per plan turn)
nso-diagnostic-run --dry-run --max-deep-checks 4 --max-handoffs 2

# disable drills
nso-diagnostic-run --dry-run --max-drill-issues 0

# only if user passed publish (drop --dry-run; add --publish)
nso-diagnostic-run --skip-llm --publish [--devices DEVICES]
# with llm + publish:
nso-diagnostic-run --publish --max-drill-issues 2 --max-tools-per-drill 12 [--devices DEVICES]
```

   Full run = physical + ISIS + BGP + service/fleet (unless filtered by devices).
   Do **not** pass `--publish` unless the user included `publish` (or explicitly
   asked to deliver in chat).

4. Summarize from stdout for an impatient reader first (operator report layout):
   - Header **Result** + **Summary** (LLM bullets when present)
   - **Devices** — Attention / sync / routing highlights
   - **Services** — category table; SystemUp note when service sync skipped
     (incomplete digs keep SystemUp; dig down/degraded demote); incomplete
     checks grouped by endpoint/reason; per-instance detail only for
     digs/confirmed impairments (use `--services-detail` for every instance).
     Dig up → Passed PE-side readiness checks; Customer traffic delivery
     was not tested.
   - **Recommended follow-up** — one action per device (overlapping involvement
     counts; do not sum), then reassess distinct unknowns; combinations stay
     under Services
   - **Run details** only if useful (budgets, dry-run, LLM timeout notes)
5. Then optional deeper detail (`--full` appendix if present). Prefer grounding in
   CLI stdout; after publish, paths from stderr when printed; note LLM on/off and
   drill budgets; suggest `/nso-isis`, `/nso-bgp`, or a narrowed `/nso-service` /
   `/nso-device` follow-up when useful.
