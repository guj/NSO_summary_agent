# Try me out

Hands-on guide for running this repo. For operator details (report sections, Slack/email, scheduling, topology), see [README.md](../README.md) and [FAQ.md](FAQ.md).

```
MCP (collect) → aggregate/delta (Python) → LLM (summarize) → stdout / Slack / state/
```

## What you need

- Python **3.12+**
- A built [fabric-nso-mcp-server](https://github.com/fabric-testbed/fabric-nso-mcp-server) binary (`cisco-nso-mcp-server`)
- Reachable NSO (`NSO_ADDRESS`, credentials)
- An LLM API key for optional narrative/plans: set `FABRIC_AI_API_KEY` / `FABRIC_AI_API_URL` / `FABRIC_AI_MODEL` (OpenAI-compatible). Defaults are FABRIC AI; any compatible provider works — see [README](../README.md).

Do **not** commit or share: `.env`, `state/`, or real passwords/keys.

**Prefer Docker?** Skip the Python install below and follow [DOCKER.md](DOCKER.md) instead.

## Install

```bash
# 1) MCP server (follow that repo’s README until this works)
cd /path/to/fabric-nso-mcp-server
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
which cisco-nso-mcp-server   # note the absolute path

# 2) This agent
cd /path/to/NSO_summary_agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edit `.env` at least:

| Variable | Set to |
| -------- | ------ |
| `MCP_SERVER_CMD` | Absolute path to **your** `cisco-nso-mcp-server` |
| `NSO_ADDRESS` / `NSO_PORT` / `NSO_USERNAME` / `NSO_PASSWORD` | Your NSO |
| `NSO_VERIFY` | Optional; default verifies HTTPS (`1`). Set `0` for self-signed lab certs |
| `FABRIC_AI_API_KEY` / `URL` / `MODEL` | LLM via **OpenAI-compatible** URL only (not Anthropic `…/anthropic`). Defaults = FABRIC AI |

Then load env for the shell session:

```bash
set -a && source .env && set +a
```

## Ways to use it

### 1. Check MCP connectivity

```bash
nso-summary-run --list-tools
```

Lists tools from the NSO MCP server. If this fails, fix `MCP_SERVER_CMD` and NSO credentials before anything else.

### 2. Collect only (no LLM, no state write)

```bash
nso-summary-run --skip-llm --dry-run
```

Pulls inventory/health via MCP and prints a deterministic report. Good first end-to-end check without FABRIC AI.

### 3. Full report to stdout (no state / Slack / email)

```bash
nso-summary-run --dry-run
```

Collect → delta → FABRIC narrative → print. Does not update `state/` or send notifications.

### 4. Persist a run (and notify if configured)

```bash
nso-summary-run
```

Writes under `state/` (including `report.md`). If `SLACK_WEBHOOK_URL` and/or SMTP/`EMAIL_TO` are set, delivers the report after a successful run. See README: [Automatic Slack / email delivery](../README.md#automatic-slack--email-delivery).

### 5. Diagnostic MAS operator report (dry-run)

```bash
nso-diagnostic-run --skip-llm --dry-run
# with LLM dataplane/drill + concise operator layout:
nso-diagnostic-run --dry-run
# optional detailed-device appendix:
nso-diagnostic-run --full --dry-run
```

Prints Scope / Devices / Services / follow-up (see README § Diagnostic MAS CLI).
`--publish` delivers Slack (mrkdwn blocks) + email (HTML) and writes
`state/diagnostic_mas/`.

### 6. Run without installing the console script

```bash
python -m agent.run --dry-run
```

Same CLI flags as `nso-summary-run`.

### 7. Run the unit tests

```bash
pip install -e ".[dev]"
pytest
```

Tests live under `tests/` and are meant for **pytest**. Most use mocks — no live NSO or LLM API key required.

## Optional next steps

| Goal | Where to look |
| ---- | ------------- |
| Change which report sections appear | `REPORT_SECTIONS` in `.env` — [README § Report format](../README.md#report-format) |
| Diagnostic operator report / publish | [README § Diagnostic MAS CLI](../README.md#diagnostic-mas-cli) |
| Schedule 3× daily runs | [README § Scheduled reports](../README.md#scheduled-reports-3-daily) |
| Local Prometheus / Grafana | `deploy/monitoring/` — set `PROMETHEUS_PUSHGATEWAY_URL` for Phase 1 push |
| Topology Graphviz image | [README § Graphviz export](../README.md#graphviz-export-scriptsexport_topology_dotpy) — `scripts/export_topology_dot.py` or `dot -Tpng …` |
| LLM API key expiry / renewal | [README § FABRIC AI API key lifetime](../README.md#fabric-ai-api-key-lifetime) (default OpenAI-compatible endpoint) |
| Devices / topology questions | [FAQ.md](FAQ.md) |

## Quick success criteria

You are set up when:

1. `--list-tools` returns MCP tools  
2. `--skip-llm --dry-run` prints a report with device/service content  
3. (Optional) `--dry-run` adds FABRIC Overall Status / Action Items / Operational Assessment  
