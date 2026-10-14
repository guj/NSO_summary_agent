# NSO Summary Agent

Deterministic **MCP collection** from the Cisco NSO MCP server, plus an optional
**LLM** (OpenAI-compatible chat API) for human-readable reports and diagnostic plans.

```
MCP (collect) → aggregate/delta (Python) → LLM (summarize / plan) → stdout / Slack / state/
```

LLM settings use the env names `FABRIC_AI_API_KEY`, `FABRIC_AI_API_URL`, and
`FABRIC_AI_MODEL`. Those are the **default** values aimed at FABRIC AI
(`https://ai.fabric-testbed.net`). You can point the same three variables at
**any OpenAI-compatible** base URL, API key, and model id.
`FABRIC_AI_API_URL` must speak the OpenAI chat API (`/v1/chat/completions`);
Anthropic-only bases (for example `…/anthropic`) are not supported.

**Quick start:** follow [docs/DIAGNOSTIC_RUNNER.md](docs/DIAGNOSTIC_RUNNER.md) to install, configure, and run `nso-diagnostic-run`.

## Prerequisites

- Python 3.12+
- Running [fabric-nso-mcp-server](https://github.com/fabric-testbed/fabric-nso-mcp-server)
- Optional LLM API access via `FABRIC_AI_API_KEY` / `FABRIC_AI_API_URL` / `FABRIC_AI_MODEL` (OpenAI-compatible). Run `nso-diagnostic-run --skip-llm` without a model. Free/institutional access depends on provider eligibility and quotas; paid endpoints also work. See [model choices and our trial experience](docs/DIAGNOSTIC_RUNNER.md#running-without-an-llm-and-choosing-a-model).

## Diagnostic runner: standalone checkout

All diagnostic code, service prompts, operational checks and HTML topology assets
are included here. No other checkout is required. Install
and configure this checkout as below, then see [the diagnostic guide](docs/DIAGNOSTIC_RUNNER.md).
The external Cisco NSO MCP server must be installed separately and reachable via
`MCP_SERVER_CMD`; this repository does not vendor that server or credentials.

## Setup

Prefer Docker (no local Python venv)? See [docs/DOCKER.md](docs/DOCKER.md).

```bash
cd /path/to/checkout
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# Edit .env: NSO_PASSWORD, FABRIC_AI_API_KEY, NSO_ADDRESS, etc.
# MCP_SERVER_CMD: absolute path to cisco-nso-mcp-server if not on PATH
```

The setup uses an editable install (`-e`), so most source edits apply on the next run. Regular installs copy code into `site-packages` and must be reinstalled after source updates. See [installation modes and refresh commands](docs/DIAGNOSTIC_RUNNER.md#editable-versus-regular-installation).

## Verify LLM models (OpenAI-compatible)

Defaults target FABRIC AI; the same check works for any **OpenAI-compatible**
`FABRIC_AI_API_URL` (host root or a URL that already ends in `/v1`). Do not set
an Anthropic Messages API base here.

```bash
source .env  # or export vars manually
BASE="${FABRIC_AI_API_URL%/}"
case "$BASE" in */v1) ;; *) BASE="$BASE/v1" ;; esac
curl -s "$BASE/models" -H "Authorization: Bearer $FABRIC_AI_API_KEY" | python3 -m json.tool
```

On the default FABRIC endpoint, typical model IDs include: `gpt-oss-20b`, `qwen3-coder-30b`, `claude-haiku-4-5-20251001`, `claude-3-5-sonnet-20241022`. Set `FABRIC_AI_MODEL` in `.env` to whatever id your endpoint lists.

## FABRIC AI API key lifetime

Applies when you use the **default** FABRIC AI endpoint (or another provider that
exposes a similar `/key/info`). If you substituted a different OpenAI-compatible
URL/key, follow that provider’s key-rotation docs instead.

Every LLM-enabled run prints key expiry **and** spend allowance to **stderr**
before collection (from `GET /key/info` at the provider's root, so a
`FABRIC_AI_API_URL` that ends in `/v1` works too):

```text
FABRIC AI API key lifetime: expires 2027-01-21 00:00 UTC — 125 days remaining (/key/info)
FABRIC AI API allowance: spend=42.5; max_budget=50; 7.5 remaining; resets 2026-10-01 00:00 UTC (/key/info)
FABRIC AI LLM model: requested=gpt-oss-20b resolved=gpt-oss-20b (chat/completions)
```

If spend already meets/exceeds `max_budget`, LLM calls are skipped for that run.
If a mid-run response returns `budget_exceeded`, further LLM digs/drills/summary
stop immediately and completed work is preserved.

The model line probes a 1-token chat completion so `resolved=` is the id after
gateway nearest-match (not only `FABRIC_AI_MODEL`). Skipped when the key is
already expired, auth-invalid, or spend-budget exhausted.

Lookup order:

1. `**GET /key/info**` at the provider's root, which is `FABRIC_AI_API_URL` without a trailing `/v1` (LiteLLM; uses your `FABRIC_AI_API_KEY` as Bearer token) — use the `**expires**`, `**spend**`, and `**max_budget**` fields
2. `**.env` fallback** when `/key/info` is unavailable:
  - `FABRIC_AI_KEY_EXPIRES=2026-07-01`, or
  - `FABRIC_AI_KEY_CREATED=2026-06-25` + `FABRIC_AI_KEY_LIFETIME_DAYS=30` (approximate only)

### Calendar-month expiry (not always rolling 30 days)

Credential Manager may describe keys as “30 day” duration, but FABRIC AI often sets `**expires` to the start of the next calendar month** (`YYYY-MM-01T00:00:00+00:00`), not `created_at + 30 days`.

Example: key created **2026-06-25** → `**expires` 2026-07-01** (~6 days of use, not 30).

Always trust `**expires`** from `/key/info`. Keys created late in the month have **short remaining life** even when the UI mentions 30 days.

Renew at [cm.fabric-testbed.net](https://cm.fabric-testbed.net) before `expires` — many operators roll keys at the **start of each month**.

Warnings appear when the key expires within 7 days or is already expired.

### Renew / roll keys (you cannot extend in place)

LLM API keys are issued with a fixed TTL (**1–30 days**, max **30** per key). There is **no renew/extend** API for an existing `sk-...` key — create a **new** key before the old one expires, update `.env`, then delete the old key in Credential Manager.

**Check current expiry**

```bash
set -a && source .env && set +a

# The provider's root: the URL without a trailing slash or /v1
ROOT="${FABRIC_AI_API_URL%/}"; ROOT="${ROOT%/v1}"

# Preferred: server metadata (FABRIC AI / LiteLLM)
curl -s "$ROOT/key/info" \
  -H "Authorization: Bearer $FABRIC_AI_API_KEY" | python3 -m json.tool

# Sanity check the key still works
curl -s "$ROOT/v1/models" \
  -H "Authorization: Bearer $FABRIC_AI_API_KEY" | python3 -m json.tool
```

Look for `"expires"` in the `/key/info` JSON. If `/key/info` is unavailable, use the `.env` fallback vars documented above or the LLM keys list in Credential Manager.

**Create a new key**

1. **Web UI:** [cm.fabric-testbed.net](https://cm.fabric-testbed.net) → log in (CILogon) → **LLM tokens / API keys** → create key → duration **30** days → copy the new `sk-...`.
2. **API** (after CM login; use your CM `id_token` as Bearer):

```bash
export CM_ID_TOKEN='...'   # from Credential Manager / portal login

# List existing LLM keys (optional)
curl -s "https://cm.fabric-testbed.net/credmgr/tokens/llm_keys" \
  -H "Authorization: Bearer $CM_ID_TOKEN" | python3 -m json.tool

# Create a new 30-day key
curl -s -X POST \
  "https://cm.fabric-testbed.net/credmgr/tokens/create_llm?key_name=nso-summary&duration=30&comment=NSO%20summary%20agent" \
  -H "Authorization: Bearer $CM_ID_TOKEN" \
  -H "accept: application/json" | python3 -m json.tool
```

The response includes the new key (commonly under `data[0].details.api_key`).

**Update this project**

```bash
# In .env
FABRIC_AI_API_KEY=sk-NEW_KEY_HERE
# Optional fallback for stderr lifetime line if /key/info fails:
# FABRIC_AI_KEY_CREATED=2026-06-29
# FABRIC_AI_KEY_LIFETIME_DAYS=30
```

If you run on a schedule (cron/launchd), ensure that job sources the same `.env` file.

**Verify**

```bash
set -a && source .env && set +a
nso-diagnostic-run --dry-run
# stderr shows the new expiry before collection starts
```

**Clean up:** delete the old key in Credential Manager after the new key works, so you are not unsure which key is active.

**Reminder:** plan to roll keys **at least monthly** (or when the agent warns ≤7 days remaining). A new key’s `expires` follows FABRIC’s current policy (often **next month boundary**), not necessarily 30×24h from when you click create.

## Run

```bash
# Check the MCP server, NSO login, LLM key and delivery settings before a scan
nso-diagnostic-run --check-connection

# Live collection and operational checks; no LLM, nothing saved or sent
nso-diagnostic-run --skip-llm --dry-run

# LLM-assisted diagnosis, report to stdout only
nso-diagnostic-run --dry-run

# One service (replace with its exact ID)
nso-diagnostic-run --service-only --service-id SERVICE_ID --dry-run

# Save the case and reports, and deliver to configured Slack/email
nso-diagnostic-run --publish
```

Dry-run still contacts NSO and, unless skipped, the LLM. See [Diagnostic MAS CLI](#diagnostic-mas-cli)
for flags and [docs/DIAGNOSTIC_RUNNER.md](docs/DIAGNOSTIC_RUNNER.md) for the full guide.

Without the console script, from the checkout root:

```bash
python -m diagnostic_mas.run --dry-run
```

## Local Prometheus + Grafana

Metrics stack (Pushgateway, Prometheus, Grafana) lives under **`deploy/monitoring/`** with its own setup guide:

```bash
cd deploy/monitoring
docker compose up -d
```

See [deploy/monitoring/README.md](deploy/monitoring/README.md) for:

- Starting Pushgateway, Prometheus, and Grafana (`docker compose up -d`)
- Connecting Grafana to Prometheus (auto + manual)
- **Explore** walkthrough (where to type a query, **Run query**)
- Agent Phase 1 push (`PROMETHEUS_PUSHGATEWAY_URL`) and troubleshooting

## Environment variables


| Variable                      | Required | Description                                                               |
| ----------------------------- | -------- | ------------------------------------------------------------------------- |
| `NSO_PASSWORD`                | yes      | NSO admin password (passed to MCP server subprocess)                      |
| `NSO_ADDRESS`                 | yes      | NSO host                                                                  |
| `NSO_SCHEME` / `NSO_PORT` / `NSO_USERNAME` | no | Defaults `https` / `443` / `admin`                               |
| `NSO_VERIFY`                  | no       | Verify NSO HTTPS cert (default `1`). Set `0` only for self-signed lab certs |
| `NSO_CA_BUNDLE`               | no       | Path to CA bundle for private CAs (passed to MCP as `--nso-ca-bundle`)    |
| `FABRIC_AI_API_KEY`           | for LLM  | LLM bearer token (OpenAI-compatible). Default setup uses FABRIC AI; replace with your provider’s key |
| `FABRIC_AI_API_URL`           | no       | OpenAI-compatible LLM base (default `https://ai.fabric-testbed.net`; host or `…/v1`). Not Anthropic `…/anthropic` |
| `FABRIC_AI_MODEL`             | no       | Model id for that endpoint (default `gpt-oss-20b` on FABRIC AI) |
| `FABRIC_AI_KEY_EXPIRES`       | no       | Fallback expiry date if `/key/info` is unavailable (FABRIC-oriented) |
| `FABRIC_AI_KEY_CREATED`       | no       | Fallback key creation date (used with `FABRIC_AI_KEY_LIFETIME_DAYS`) |
| `FABRIC_AI_KEY_LIFETIME_DAYS` | no       | Fallback lifetime in days (default `30`) |
| `MCP_SERVER_CMD`              | no       | Path to `cisco-nso-mcp-server` binary                                     |
| `STATE_DIR`                   | no       | Default `./state`                                                         |
| `IGNORE_SERVICE_TYPES`        | no       | Comma-separated types to skip (default `idipa`; set empty to include all) |
| `NSO_SERVICE_SYNC_MODE`       | no       | `check` (default) calls `check_service_sync` per instance; `skip` uses endpoint fleet sync only (this lab). Overall status combines sync and dataplane; the diagnostic table counts endpoint sync and dataplane independently |
| `MAX_SERVICE_TYPES`           | no       | Cap on service types queried per run (default `10`; also `--max-service-types`) |
| `INTERFACE_EQUIVALENCES_FILE` | no       | JSON of admin NSO↔box interface mappings (see `config/interface-equivalences.example.json`) |
| `SLACK_WEBHOOK_URL`           | no       | Post report after successful run                                          |
| `SMTP_HOST`                   | no*      | SMTP server for email delivery                                            |
| `SMTP_PORT`                   | no       | Default `587`                                                             |
| `SMTP_USE_TLS`                | no       | Default `1` (STARTTLS)                                                    |
| `SMTP_USER` / `SMTP_PASSWORD` | no       | If your relay requires auth                                               |
| `EMAIL_FROM`                  | no*      | Sender address                                                            |
| `EMAIL_TO`                    | no*      | Comma-separated recipients                                                |
| `DRY_RUN`                     | no       | Default `1` (skip state + Slack/email). Set `0` or use `--publish` to deliver |
| `PROMETHEUS_PUSHGATEWAY_URL`  | no       | If set, push Phase 1 gauges after each successful non-dry-run             |
| `PROMETHEUS_JOB`              | no       | Pushgateway job label (default `nso-summary`)                             |
| `PROMETHEUS_INSTANCE`         | no       | Pushgateway instance label (default `default`)                            |


Required together when `EMAIL_TO` is set.

## Automatic Slack / email delivery

Each successful `nso-diagnostic-run --publish` run (or `DRY_RUN=0`) delivers to every configured channel.
Slack and email receive a compact digest. Email also carries the full HTML report as an attachment. On Slack,
a webhook cannot attach files; with a bot token the HTML report is uploaded to the channel
(see [Compact diagnostic notifications and HTML reports](#compact-diagnostic-notifications-and-html-reports)).

### Slack (incoming webhook)

1. In Slack: **Apps → Incoming Webhooks → Add to channel**
2. Copy the webhook URL into `.env`:

```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
```

1. Test the webhook (see below).
2. Run on a schedule — see [Scheduled reports](#scheduled-reports-3-daily) below.

**Test the webhook** — no NSO collect, no LLM:

```bash
cd /path/to/checkout
set -a && source .env && set +a

curl -sS -X POST -H 'Content-type: application/json' \
  --data '{"text":"NSO Summary Agent — Slack webhook test"}' \
  "$SLACK_WEBHOOK_URL"
```


| Result                                    | Meaning                                              |
| ----------------------------------------- | ---------------------------------------------------- |
| Response body `ok` and message in channel | Webhook works                                        |
| `invalid_token` or HTTP error             | Wrong or revoked URL — create a new Incoming Webhook |
| Empty `SLACK_WEBHOOK_URL`                 | `.env` not loaded; run `source .env` first           |


Or publish a real run (also posts to Slack):

```bash
nso-diagnostic-run --publish
```

Dry-run skips Slack. On success, stderr lists the channels after `Published to:`.

Treat the webhook URL like a password; anyone with it can post to that channel.

### Email (SMTP)

Add to `.env`:

```bash
SMTP_HOST=smtp.gmail.com          # or your org relay
SMTP_PORT=587
SMTP_USE_TLS=1
SMTP_USER=your-user@example.com   # login for SMTP (sender account)
SMTP_PASSWORD=your-app-password
EMAIL_FROM=nso-summary@example.com
EMAIL_TO=ops@example.com,netops@example.com
```


| Variable     | Role                                                                           |
| ------------ | ------------------------------------------------------------------------------ |
| `SMTP_HOST`  | Mail **server hostname** (e.g. `smtp.gmail.com`) — not an email address        |
| `SMTP_USER`  | Account that **logs in** to send mail (usually same as `EMAIL_FROM` for Gmail) |
| `EMAIL_FROM` | Address shown in the **From:** header                                          |
| `EMAIL_TO`   | **Recipients** of the report (can differ from `SMTP_USER`)                     |


Email settings are checked when the report is published, after collection. Setting `EMAIL_TO` without
`SMTP_HOST` and `EMAIL_FROM` makes publication fail.

#### Gmail (`smtp.gmail.com`)

Gmail requires an **App Password** when sending via SMTP with **2-Step Verification** enabled. Your normal Gmail login password will **not** work (`534 Application-specific password required` or `530 Authentication Required`).

1. Enable **2-Step Verification** on the Google account.
2. Create an **App Password**: Google Account → Security → App passwords → Mail.
3. Set in `.env`:

```bash
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USE_TLS=1
SMTP_USER=yourname@gmail.com       # Gmail account that owns the app password
SMTP_PASSWORD=abcd efgh ijkl mnop  # 16-char app password (spaces optional)
EMAIL_FROM=yourname@gmail.com      # same Gmail address
EMAIL_TO=recipient@example.com     # where reports are delivered
```

For org mail, ask IT for `SMTP_HOST` and allowed sender addresses instead of Gmail.

You can enable **both** Slack and email; the agent sends to all configured channels.

Test report without sending:

```bash
nso-diagnostic-run --dry-run
```

## Scheduled reports (3× daily)

Run the diagnostic scan automatically at fixed times. **Pick one scheduler** — cron **or** macOS launchd, not both (otherwise the job runs twice).

### Before scheduling

1. Confirm a manual published run works:

```bash
cd /path/to/checkout
source .venv/bin/activate
nso-diagnostic-run --publish
```

1. Create a log directory:

```bash
mkdir -p logs
```

1. Scheduled runs must deliver notifications: set `DRY_RUN=0` in the job env, or pass `--publish`. Default `DRY_RUN=1` skips state updates and Slack/email.

### Option A — cron (Linux / macOS)

`crontab -e` only **opens the editor**. The schedule line goes **inside** the file you edit — it is not part of the shell command.

**Step 1 — open crontab:**

```bash
crontab -e
```

**Step 2 — paste this single line** (adjust path if your install differs):

```cron
0 8,14,20 * * * cd /path/to/checkout && . .venv/bin/activate && set -a && source .env && set +a && nso-diagnostic-run --publish >> logs/run.log 2>> logs/run.err
```

**Step 3 — save and quit** the editor (`:wq` in vim, or Ctrl+O then Ctrl+X in nano).

**Step 4 — verify:**

```bash
crontab -l
```

**Schedule fields** (`0 8,14,20 `* * *):


| Field        | Value     | Meaning             |
| ------------ | --------- | ------------------- |
| Minute       | `0`       | Top of the hour     |
| Hour         | `8,14,20` | 08:00, 14:00, 20:00 |
| Day of month | `*`       | Every day           |
| Month        | `*`       | Every month         |
| Day of week  | `*`       | Every weekday       |


Cron does **not** use `deploy/com.esnet.nso-summary.plist`.

### Option B — macOS launchd

Example plist: `deploy/com.esnet.nso-summary.plist` (same 8:00 / 14:00 / 20:00 schedule).

```bash
mkdir -p logs
cp deploy/com.esnet.nso-summary.plist ~/Library/LaunchAgents/net.esnet.nso-summary-agent.plist
# Edit paths in the plist if your install directory differs

launchctl load ~/Library/LaunchAgents/net.esnet.nso-summary-agent.plist
```

Check logs:

```bash
tail -f logs/run.log logs/run.err
```

Unload:

```bash
launchctl unload ~/Library/LaunchAgents/net.esnet.nso-summary-agent.plist
```

Launchd does **not** require a crontab entry. `.env` in the project directory is loaded automatically by the agent.

### What each scheduled run does

1. Collects inventory and endpoint sync from NSO through MCP, then runs the deterministic operational checks
2. Investigates eligible services with the LLM (skipped with `--skip-llm`)
3. Compares the result with the last published case
4. Saves `case.json`, `report.md` and `report.html` under `state/diagnostic_mas/runs/<run-id>/`
5. Sends the digest to email and/or Slack if configured

## Service health status

Each service **instance** has a sync-layer **system status** (`up` / `degraded` /
`unknown`) and an optional **dataplane** status after dig. Overall `status` is
the worse of the two (incomplete digs do **not** demote — see Diagnostic MAS
below). The diagnostic category table has two independent groups after Total:
**Configuration sync** (In / Out / Unknown) and **Dataplane** (Up / Down /
Degraded / Unknown / Not checked). Each group sums to Total. Sync counts use
all endpoint fleet-sync results: any confirmed out-of-sync endpoint means Out;
otherwise missing/unresolved endpoints mean Unknown; all in-sync means In.
A dataplane fault does not alter configuration sync. Dataplane Unknown means an
inconclusive investigation; Not checked means no investigation. Collection's
default dataplane status does not count as a verified Up. Up denotes PE-side
readiness, not tested customer traffic delivery.

The HTML table includes a collapsed **Status definitions** section:
- **Dataplane Up:** required PE-side checks passed; customer delivery was not tested.
- **Down:** evidence confirms a failed component or forwarding path required by the service.
- **Degraded:** evidence confirms partial impairment while some service functionality remains available.
- **Unknown:** investigation was attempted, but evidence is insufficient or contradictory; timeout alone does not mean Down.
- **Not checked:** no dataplane investigation was performed this run.

Configuration-sync **In** means all endpoints report in-sync; **Out** means at
least one reports out-of-sync; **Unknown** means no confirmed out-of-sync
endpoint but missing or unresolved endpoint evidence.

Implementation: `nso_facts/health.py` (`classify_system_status`,
`combine_service_status`). Tests: `tests/test_health.py`.

### Data sources


| Source              | MCP tool                 | What it provides                                                                                                               |
| ------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| Service intent sync | `check_service_sync`     | Whether NSO's configured intent for that instance is applied (`in_sync` / `result: in-sync`) — used when `NSO_SERVICE_SYNC_MODE=check` |
| Device sync (fleet) | `get_fleet_sync_summary` | Per-device `in-sync` / `out-of-sync` / `error` for backing devices (one batch per run; not per-device `check_device_sync`)     |


**`NSO_SERVICE_SYNC_MODE`:** `check` (default) calls `check_service_sync` per
instance and falls back to endpoint fleet sync when service sync is missing or
inconclusive. `skip` (this lab) never calls `check_service_sync` and classifies
system status from endpoint fleet sync only — one note under the Services table.

Backing devices are read from the service instance YANG (top-level `device`,
nested containers such as `stp-a` / `stp-z` on `l2ptp`, etc.).

### Sync-layer classification (`system_status`)

Sync classification never sets **down**. Down requires positive evidence a
required service path failed (dataplane / live checks), not a query miss.


| Status       | Criteria                                                                                                                        |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| **unknown**  | Endpoint sync/query gap (timeout, tool error, unreachable), `check_service_sync` error / null `in_sync`, or no usable signal  |
| **degraded** | Service intent **out of sync**, or any known backing device **out-of-sync** (config drift — not automatically a forwarding outage) |
| **up**       | Service sync **in sync**, **or** (when service sync skipped/null) all known backing devices **in-sync** via fleet summary         |


Notes:

- **unknown** describes insufficient evidence, not a confirmed outage.
- **degraded** means intent/config drift — not necessarily a hard outage.
- In-sync does not prove forwarding works (that is the dataplane dig).

Types listed in `IGNORE_SERVICE_TYPES` (default: `idipa`) are skipped entirely — no health checks or counts.

## Network topology

The HTML report contains two collapsible topology views built from the run's evidence, with no extra queries:
a routing view (IS-IS and BGP relationships between devices) and a service view (which devices each service
attaches to, coloured by its final status). See [Reports and topology](docs/DIAGNOSTIC_RUNNER.md#reports-and-topology)
and [Service topology in HTML reports](docs/DIAGNOSTIC_RUNNER.md#service-topology-in-html-reports).

Design notes for the topology facts layer are in [nso_facts/topology/docs/DESIGN.md](nso_facts/topology/docs/DESIGN.md).
Operator questions about counts and unmapped neighbors are in [docs/FAQ.md](docs/FAQ.md).

## Diagnostic MAS CLI

Blackboard coordinator with ISIS / BGP / Service / Device roles: **`nso-diagnostic-run`**.

- Design: [Diagnostic runner guide](docs/DIAGNOSTIC_RUNNER.md)
- Evidence / diagnoses only from MCP + attributed conclusions; state under `state/diagnostic_mas/` (skipped in dry-run)
- Default is dry-run; use `--publish` (or `DRY_RUN=0`) for Slack/email + artifacts

### Operator report (default)

Stdout / `report.md` use a concise operator layout (not the older Issues/Budget dump):

1. **Header** — Scope (devices · services), Duration, Result  
2. **Summary** — LLM prose only (when LLM is enabled); omitted on `--skip-llm`  
3. **Devices** — per-device sync / health / BGP·IS-IS / Attention (deterministic)  
4. **Services** — category table with Total and grouped configuration-sync / dataplane
   columns (independent counts); DataplaneDig line; incomplete collection checks grouped by
   endpoint/reason; per-instance sections only for digs / confirmed impairments
   (`--services-detail` for every instance). Dig `dataplane=up` renders as
   "Passed PE-side readiness checks. Customer traffic delivery was not
   tested." (no idle-customer / outside-scope inference).  
5. **Recommended follow-up** — confirmed service faults first, one action per group
   of faulted services that share a service type and location; then one action per
   device (overlapping involvement counts; do not sum), then reassess distinct
   unknowns; endpoint combinations stay under Services  
6. **Run details** — evidence counts, budgets, dry-run / reporting notes  

In the HTML report, each device also has a collapsed **Interfaces and hardware** block (interface counts, hardware readings, exceptions). It is not part of the terminal or Slack text.

A port investigation adds a **For the technician** list to that block: each down link with its description as the device shows it, how long the device says it has been down, each service on it with where else that service attaches, and the show commands the light levels and link states were read with. All of it comes from that scan's own readings and service definitions.

The section of an l3rt service that is OpUp ends with a **Delivery check for an operator**: the ping to its gateway that someone on the customer side can run, from a permitted host when the service lists one. The line says the agent did not run it; the agent has no access to customer hosts.

**Publish:** Slack and email receive a compact digest; email attaches the self-contained HTML report (see [Compact diagnostic notifications and HTML reports](#compact-diagnostic-notifications-and-html-reports)).

### Useful flags

| Flag | Role |
|------|------|
| `--skip-llm` | Spines only; no dataplane/drill/summary LLM |
| `--deterministic-summary` | Skip final Summary chat; restate dataplane diagnoses/findings (default is LLM Summary) |
| `--service-type` / `--service-id` | **Service-first** (implies lean service-only): match instances, basic checks for all; with LLM enabled, an explicit `--service-id` also investigates instances whose basic checks pass (within budget); type-only focus investigates suspicious instances; no whole-NSO topology unless evidence expands. Combined = intersection; exit if nothing matches |
| `--max-dataplane-tools N` | Per-service dataplane verify budget (default 40; `0` skips) |
| `--max-dataplane-services N` | Total cap on dataplane LLM instances (default without per-category: one best per typed prompt; **two** if only one typed category is present) |
| `--max-dataplane-per-category N` | Up to N digs from each typed prompt category (even overnight sample; optional total cap still applies) |
| `--max-drill-issues N` | Run-wide drill slot count (default 2; separate from dataplane selection) |
| `--max-port-investigations N` | Down ports and cut-off devices investigated once before the service investigations (default 6; 0 investigates every service individually) |
| `--max-port-counter-reads N` | Ports whose error counters are read each scan and compared with the next: ports under a service or an IS-IS adjacency (default 400; 0 reads none) |
| `--max-tools-per-drill N` | MCP calls per drilled issue (default 12) |
| `--services-detail` | Emit a per-instance section for every service (default: digs / impairments only) |

```bash
# Spines only (MCP findings; no autonomous diagnosis)
nso-diagnostic-run --skip-llm --dry-run

# Service-first (all l2ptp; LLM only on suspicious)
nso-diagnostic-run --service-type l2ptp --dry-run

# One instance
nso-diagnostic-run --service-id fabric-l2ptp-t1 --dry-run

# Full diagnosis (requires LLM API key + model via FABRIC_AI_* — any OpenAI-compatible values)
nso-diagnostic-run --dry-run

# Deliver Slack/email + state/diagnostic_mas/
nso-diagnostic-run --publish

# Compare two published runs (new / recovered / persistent / coverage)
nso-diagnostic-delta state/diagnostic_mas/runs/<older> state/diagnostic_mas/runs/<newer>
```

## Project layout

```
nso_facts/        # MCP collection, normalization, topology, health, metrics (no LLM)
nso_report/       # deterministic device/executive formatters (no LLM)
diagnostic_mas/   # nso-diagnostic-run: coordinator, operational checks, dataplane digs, drills, reports
  prompts/        # runtime prompts, including service-specific dataplane prompts
agent/            # shared config, LLM client, Slack/email publishing, markdown rendering
multi_agent/      # shared spine collection, deep checks and tool gating
docs/
  DIAGNOSTIC_RUNNER.md  # install, configure, run, read the report
  DOCKER.md             # build and run in a container
  FAQ.md                # operator FAQ
state/            # gitignored; diagnostic_mas/runs/<run-id>/ holds case.json, report.md, report.html
deploy/
  com.esnet.nso-summary.plist   # example launchd schedule
  monitoring/                   # docker compose, Prometheus and Grafana dashboards
```

## Diagnostic report details

### Diagnostic fault history and follow-up

Diagnostic reports prioritize current service faults and unresolved last-known
faults before collection gaps and inventory observations. A new down/degraded
finding is labeled **newly detected**, not a proven regression: an earlier
readiness pass may have used different checks.

Published diagnostic cases persist `last_known_service_faults` with their last
observation run and explanation. Sampling omissions, a narrower scan, baseline
sync passes, and inconclusive rechecks do not clear those findings. A completed
current dataplane pass clears the historical fault, and so does a pass of the
service's own operational checks in the current run; current faults refresh it.
A fault is dropped when a full, unfiltered scan no longer lists the service; the
report then lists it under services no longer present.
Historical findings are reported separately and do not alter current-run service
counts or masquerade as fresh evidence. Old cases without the field are supported.
Dry-runs can show history from the last published case but do not persist updates.

An LLM "Up" is accepted only when the service's operational check got a usable answer
from every device it asked. Otherwise the service stays Unknown and the LLM's finding
is kept as a note; a fault found the same way is still reported. A service investigated
in two scans whose operational check read the same, but whose final status differs, is
listed under **Status changed while the fixed checks read the same**.

Each open fault also carries the scan that first listed it, and keeps it while the
fault stays open; a fault that clears and returns starts again. A fault carried
from a scan that did not record this is dated once from the saved scans under
`runs/`. When the oldest saved scan already lists it, the report says "or earlier".
For a down link behind a port finding, the report adds the time the device itself
gives ("Last link flapped"), read from that scan's own port investigation and never
from an earlier scan. **Changes since previous run** lists these under
**Service faults still open**, one line per port or cut-off device that explains
several services, and one line per reason for services the fixed check decided
Down for the same reason. A fault found in the current scan is listed as new, not as still open.


### Links gaining errors while up

Each scan reads the error counters of every port that carries a service attachment or
an IS-IS adjacency: one `show interfaces <port>` per port, in the collection phase after
the service step, within `--max-port-counter-reads`. The readings are saved with the scan.
The next scan subtracts, and **Changes since previous run** lists a port under **Links
gaining errors while up** when it is up now and its CRC, input-error or output-error
count grew, or its state-transition count grew (it went down and came back). The list is
ordered by size: most errors gained first, then links that only changed state. The Slack and
email digest carries the three largest, one per line.

This is a lead, never a status: no service changes state because of it. Drops alone are
not listed. A port is not compared when it was not read in both scans, when its counters
were cleared in between, or when a counter went back. A read that fails leaves the port
without a reading and is not a fault. The first scan after enabling this has nothing to
subtract.

### Undecided rules

Two rules for l3rt are waiting for the operators to decide. Until then the report
shows how many services each would change, in an **Undecided rules** block after
**Changes since previous run**. The block is left out when both counts are zero,
and no status in the report depends on it.

- **Border route:** l3rt that are Up on the LLM's conclusion where the fixed check's
  only gap is the service's exact prefix on a border router. If the exact prefix is
  required they become Unknown; if a covering route is enough they pass without the LLM.
- **Cut-off device:** for a device with no IS-IS adjacency and no BGP session up, the
  services its cut-off investigation marked Down, and the l3rt that name only that
  device and are still Up. Those would be Down if an l3rt must reach other sites.

The Slack and email digest shows the cut-off line whenever a device is cut off, and
the border-route line only when its count differs from the previous scan. The counts
are saved with the scan as `undecided_rules`. All of this lives in
`diagnostic_mas/undecided_rules.py`; remove a rule's count there once the rule is
decided and built into the fixed checks.

### LLM usage, cost and budget

A scan that uses the LLM reports what it used, in two layers.

- **Token counts, with any OpenAI-compatible endpoint.** Every answer carries its
  input and output token counts; the scan adds them up, the start-up model probe
  included. Run details show `LLM usage: N requests · X input tokens · Y output tokens`.
- **Cost and budget, when the provider reports them.** A LiteLLM proxy serves
  `GET /key/info` and `GET /user/info` at its root. The scan reads them before its
  first request and after its last. The start of the scan prints one line to stderr,
  for example `LLM budget: $280.54 of $500.00 used (1mo), $219.46 left, resets 2026-11-01.`
  Run details then show the scan's cost, which is the key's spend at the end less
  its spend at the start, and the budget that is left. The budget is the key's own
  when it has one, otherwise its user's.

With any other provider the cost line reads `LLM cost: not reported by this provider`
and the token counts are still shown. Two limits: the cost is the key's spend, so
anything else using the same key during the scan is included, and a proxy records
spend shortly after each request, so the last requests may not be counted yet.
The record is saved with the scan as `llm_usage` in `case.json`.
`nso-diagnostic-run --check-connection` prints the budget line too. A failed
read never stops a scan.

### Dataplane investigation gaps and timing

Each new dataplane diagnosis stores `investigation` telemetry: elapsed LLM
requests (including failures/retries), MCP execution time, other overhead,
rounds, tool usage/remaining allowance, and the termination reason.
MCP elapsed time includes the client execution wrapper, not solely server time;
LLM elapsed time includes provider/network waiting, not just model reasoning.
Run details show one timing line per investigated service.

Unknown conclusions may include `verification_gap`: blocker, missing check,
direction, next check, and required access. These are attributed to the LLM;
runner interruptions receive a conservative runner-attributed gap. Structured
gaps override legacy prose heuristics. Customer-host SSH and traffic tests
remain operator follow-up outside this agent. Existing cases without these
fields still render, but historical timings cannot be reconstructed.


### Preserve MCP responses

Add `--save-mcp-results` to any `nso-diagnostic-run` invocation to archive
complete responses received by the client before local evidence/LLM clipping.
It works with `--dry-run` and does not enable Slack/email or update latest.json.
The CLI prints the unique JSONL path under the diagnostic state directory's
`mcp-results/` folder. Each record includes timestamp, tool, arguments, response,
raw MCP response for wire calls, and source (wire/cache/quarantine/transport error).
Files are private (0600); they may contain full device configuration and should
not be committed. No environment variables or client credentials are added.
Server-side truncation cannot be undone. Default runs do not create this archive.

```bash
nso-diagnostic-run --service-only --service-id <service-id> --dry-run --save-mcp-results
```


### Compact diagnostic notifications and HTML reports

Published `nso-diagnostic-run` runs now save `report.html` alongside `report.md`
and `case.json`. Email and Slack receive a bounded digest instead of the full
report. The digest leads with the status counts, then what changed since the
previous scan (new service faults, cleared faults, devices answering again, links
that went down, routing sessions lost) and the faults still open with their age.
The priority follow-up items come after that. The rest of the Result line and the
investigation-coverage sentence are in the full report only. Email includes the self-contained HTML as an attachment: download it
and open it in a browser for search, service-status filters, expandable details,
and section navigation. No additional LLM calls are used. Terminal Markdown is
unchanged. Dry runs do not publish or save the HTML report.

Slack file attachments require `SLACK_BOT_TOKEN` with `files:write` and
`SLACK_CHANNEL_ID`. The bot must be a member of that channel: invite it with
`/invite @your-app-name`. The upload uses Slack's
getUploadURLExternal → upload → completeUploadExternal flow. If the upload fails with
`not_in_channel`, the bot has not been invited to that channel. Existing webhook-only
setups still receive a compact digest, but cannot attach the HTML. Optionally set
`DIAGNOSTIC_REPORT_BASE_URL` to an existing internal HTTPS location serving the
contents of the diagnostic `runs/` directory; webhook posts will link to
`<base>/<run-id>/report.html`. The runner does not deploy or host those files.
Without bot upload or hosting, full details remain in run artifacts and the email
attachment. HTML attachments may require downloading or may be blocked by mail policy.

### Run configuration snapshot

HTML reports include a collapsed **Run configuration** section near Run details.
The runner captures effective model/provider host, scope, sampling, budgets,
known timeouts, service-sync and publishing/report modes before collection and
saves them in `case.json`. Only selected non-secret settings are included; API
keys, passwords, webhook URLs and full MCP command/environment are excluded.
Server-controlled timeouts not supplied by the runner are marked unrecorded.
Older reports without a snapshot do not substitute current environment values.

### Optional LLM temperature

`FABRIC_AI_TEMPERATURE` controls all LLM requests, including dataplane,
drills, planning, agent follow-ups and summaries. Unset defaults to **0.1**.
Set `FABRIC_AI_TEMPERATURE=1` for a model requiring 1. An explicitly blank
`FABRIC_AI_TEMPERATURE=` omits the API parameter and uses the provider default.
Numeric values must be finite and between 0 and 2; individual models may
restrict that range further. Run configuration records the effective value
or "Provider default (parameter omitted)". Existing `.env` values are preserved.
