# Multi-agent CLI (IS-IS + BGP + devices)

Config-driven orchestrator with **IsisAgent**, **BgpAgent**, and **DeviceAgent**.  
Sibling to production `nso-summary-run` — same `.env` / MCP / publish stack, **independent** state tree.

**Specs:**
- Production MVP: [docs/superpowers/specs/2026-08-10-multi-agent-production-mvp-design.md](../docs/superpowers/specs/2026-08-10-multi-agent-production-mvp-design.md)
- Original design: [docs/superpowers/specs/2026-07-28-multi-agent-isis-bgp-design.md](../docs/superpowers/specs/2026-07-28-multi-agent-isis-bgp-design.md)

**Human operating SOP:** [HUMAN_OPERATING_SOP.md](HUMAN_OPERATING_SOP.md)

## What it does

1. Same `.env` as the summary agent (MCP, Fabric, Slack/email).
2. **IsisAgent** — bidirectional IS-IS spine; keeps edges + issues.
3. **BgpAgent** — bidirectional BGP spine; keeps edges + issues.
4. **DeviceAgent** (default: devices that appear on ISIS/BGP issues with `edge_id`):
   - **Seed** from topology slice
   - **Hardware spine** via `get_hardware_health`
   - Optional Fabric plan → gated MCP with **device forced**
5. Merge → optional Fabric summary → stdout.
6. Slack/email + `state/multi_agent/` only with `--publish` or `DRY_RUN=0` (default `DRY_RUN=1`).

Does **not** read or write `state/latest.json` used by `nso-summary-run`.

## State (deliver only)

| Path | Purpose |
|------|---------|
| `state/multi_agent/latest.json` | Counts/services for next-run delta |
| `state/multi_agent/runs/<run_id>/` | Artifacts (`isis.json`, `bgp.json`, `merged.json`, `report.md`, …) |

Dry-run: stdout only — no `state/multi_agent/` writes.

## Run

```bash
cd /path/to/NSO_summary_agent
source .venv/bin/activate
pip install -e .
set -a && source .env && set +a

nso-multi-agent-run --isis-only --spine-only --skip-devices
nso-multi-agent-run --bgp-only --spine-only --skip-devices
nso-multi-agent-run --spine-only
nso-multi-agent-run --devices lbnl-data-sw,renc-data-sw
nso-multi-agent-run --publish
```

Equivalent: `python -m multi_agent.run …`  
Deprecated shim: `python -m experiments.multi_agent.run …` (prints a warning, then forwards).

## Claude Code slash commands

With Claude Code opened on this repo:

| Command | What it runs |
|---------|----------------|
| `/nso-isis` | `--isis-only` spine-only check |
| `/nso-bgp` | `--bgp-only` spine-only check |
| `/nso-multi-agent` | Full multi-agent report (`publish` optional) |

See `CLAUDE.md` and `.claude/commands/`.

## Report shape

- **Overall Status** / **Fleet Summary** / **Action Items** / optional **Operational Assessment** (top)
- IS-IS / BGP detailed sections (bidirectional totals + issues)
- **Devices** — per selected device: topology seed, hardware, issues, evidence preview
- Fleet delta section when fleet spine ran (vs prior **multi-agent** `latest.json`)
