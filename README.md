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

See [WORK_PLAN.md](WORK_PLAN.md) for the full roadmap.

**New here?** Start with [docs/TRY_ME_OUT.md](docs/TRY_ME_OUT.md) — install, smoke checks, and common ways to run the agent. Prefer containers? See [docs/DOCKER.md](docs/DOCKER.md).

## Prerequisites

- Python 3.12+
- Running [fabric-nso-mcp-server](https://github.com/fabric-testbed/fabric-nso-mcp-server) (same binary as Cursor `myNso` MCP)
- LLM API access via `FABRIC_AI_API_KEY` / `FABRIC_AI_API_URL` / `FABRIC_AI_MODEL` (OpenAI-compatible; FABRIC AI is the default free endpoint, or substitute your own)

## Setup

Prefer Docker (no local Python venv)? See [docs/DOCKER.md](docs/DOCKER.md).

```bash
cd /path/to/NSO_summary_agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# Edit .env: NSO_PASSWORD, FABRIC_AI_API_KEY, NSO_ADDRESS, etc.
# MCP_SERVER_CMD: absolute path to cisco-nso-mcp-server if not on PATH
```

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
before collection (from `GET {FABRIC_AI_API_URL}/key/info`):

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

1. `**GET {FABRIC_AI_API_URL}/key/info**` (LiteLLM; uses your `FABRIC_AI_API_KEY` as Bearer token) — use the `**expires**`, `**spend**`, and `**max_budget**` fields
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

# Preferred: server metadata (FABRIC AI / LiteLLM)
curl -s "${FABRIC_AI_API_URL%/}/key/info" \
  -H "Authorization: Bearer $FABRIC_AI_API_KEY" | python3 -m json.tool

# Sanity check the key still works
curl -s "${FABRIC_AI_API_URL%/}/v1/models" \
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
nso-summary-run --dry-run
# stderr should show the new expiry and the run should complete
```

**Clean up:** delete the old key in Credential Manager after the new key works, so you are not unsure which key is active.

**Reminder:** plan to roll keys **at least monthly** (or when the agent warns ≤7 days remaining). A new key’s `expires` follows FABRIC’s current policy (often **next month boundary**), not necessarily 30×24h from when you click create.

## Run

```bash
# List MCP tools (connectivity check)
nso-summary-run --list-tools

# Collect only — no LLM, no state update
nso-summary-run --skip-llm --dry-run

# Full run: collect → delta → FABRIC summary → save state/
nso-summary-run

# Print summary but do not write latest.json or Slack
nso-summary-run --dry-run
```

Or without install:

```bash
python -m agent.run --dry-run
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

## Report format

### `nso-summary-run`

Reports use a **fixed layout** every run. Python builds the structured sections deterministically; the LLM writes only the **Problems / Failures** narrative (`temperature=0`).

**Delivery formats:**


| Channel                | Format                                                   |
| ---------------------- | -------------------------------------------------------- |
| Terminal / `report.md` | Markdown (`**bold`** headers, pipe table for counts)     |
| Slack                  | Incoming-webhook **mrkdwn** section blocks (from markdown / plain) |
| Email                  | Multipart: plain text + HTML (tables / headings)         |


Example (terminal / `report.md`):

```
**NSO Ops Snapshot — {run_id}**

**Problems / Failures**
{LLM narrative, or "None reported."}

**Service Counts (by type)**
| Type | Total | Up | Down | Degraded | Unknown |
| --- | ---: | ---: | ---: | ---: | ---: |
| l2ptp | 2 | 2 | 0 | 0 | 0 |
...

**Delta since last run**
No delta to report

**Fleet sync**
...
```

Example (Slack / email plain part):

```
NSO Ops Snapshot — {run_id}
========================================

Problems / Failures
--------------------
...

Service Counts (by type)
--------------------
Type            Total   Up  Down  Degr  Unkn
--------------------------------------------
l2ptp               2    2     0     0     0
...
```

- **Service counts** — one row per service type, sorted alphabetically (`agent/report_format.py`).
- **Delta since last run** — `No delta to report` when counts and status are unchanged; `First run — no previous snapshot to compare.` on the first successful run.
- **Devices** — per-device rollups for interfaces / BGP peers / IS-IS adjacencies; lists only non-`up` exceptions; unmapped CLI neighbors called out (`agent/report_devices.py`). See [FAQ](docs/FAQ.md).
- **Ignored types** — listed when `IGNORE_SERVICE_TYPES` is set and `ignored_types` is included in `REPORT_SECTIONS`.

### Report sections (`REPORT_SECTIONS`)

Comma-separated allowlist controlling which blocks appear (and in what order) for markdown, Slack plain text, and email HTML/plain.

| Name | Heading | Source |
|------|---------|--------|
| `executive` | Executive Summary | FABRIC status/actions/assessment + Python Fleet Summary/tables |
| `problems` | Problems / Failures | FABRIC AI (optional; omitted from default) |
| `counts` | Service Counts (by type) | Snapshot counts (optional; in exec by default) |
| `delta` | Delta since last run | Delta vs previous (optional; Changes in exec) |
| `fleet_sync` | Fleet sync (legacy one-liner) | Opt-in; Exec **Fleet Summary** is default |
| `system_health` | Infrastructure Health | Opt-in FABRIC narrative (CPU/memory); default uses Fleet Summary alerts |
| `devices` | Detailed Device Analysis / Devices | Operational topology |
| `ignored_types` | Ignored service types | Configured ignore list |

Default when unset: `executive,devices,ignored_types`.

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
| `NSO_SERVICE_SYNC_MODE`       | no       | `check` (default) calls `check_service_sync` per instance; `skip` uses endpoint fleet sync only (this lab). SystemUp starts from that sync; incomplete digs keep SystemUp; dig-confirmed down/degraded demote |
| `MAX_SERVICE_TYPES`           | no       | Cap on service types queried per run (default `10`; also `--max-service-types`) |
| `REPORT_SECTIONS`             | no       | Ordered section allowlist (includes `system_health`, `devices`; see above) |
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

Yes — each successful `nso-summary-run` (not `--dry-run`) sends the report automatically when configured.

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
cd /Users/dec2023/Work/ESnet/NSO_summary_agent
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


Or run a full report (also posts to Slack):

```bash
nso-summary-run
```

Do **not** use `--dry-run` — dry-run skips Slack. On success, stderr shows `Delivered via: slack`.

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


Test delivery:

```bash
nso-summary-run --test-email
```

On each normal run, email settings are validated early when `EMAIL_TO` is set (before MCP collection).

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

Use `--test-email` after updating `.env`. For org mail, ask IT for `SMTP_HOST` and allowed sender addresses instead of Gmail.

You can enable **both** Slack and email; the agent sends to all configured channels.

Test report without sending:

```bash
nso-summary-run --dry-run
```

## Scheduled reports (3× daily)

Run the summary automatically at fixed times. **Pick one scheduler** — cron **or** macOS launchd, not both (otherwise the job runs twice).

### Before scheduling

1. Confirm a manual run works (no `--dry-run` if you want email/Slack delivery):

```bash
cd /Users/dec2023/Work/ESnet/NSO_summary_agent
source .venv/bin/activate
nso-summary-run
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
0 8,14,20 * * * cd /Users/dec2023/Work/ESnet/NSO_summary_agent && . .venv/bin/activate && set -a && source .env && set +a && nso-summary-run >> logs/run.log 2>> logs/run.err
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

1. MCP collect from NSO
2. Delta vs `state/latest.json`
3. FABRIC AI summary (Problems section; table and delta are fixed format)
4. Email and/or Slack if configured
5. Updates `state/latest.json` for the next run

## Service health status

Each service **instance** has a sync-layer **system status** (`up` / `degraded` /
`unknown`) and an optional **dataplane** status after dig. Overall `status` is
the worse of the two (incomplete digs do **not** demote — see Diagnostic MAS
below). Type-level **SystemUp** / down / degraded / unknown in the diagnostic
table are rollups of that overall status.

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

### Snapshot fields

Per instance (`snapshot.services["{type}/{name}"]`):

```json
{
  "service_type": "l2ptp",
  "name": "fabric-l2ptp-test",
  "devices": ["renc-data-sw", "uky-data-sw"],
  "in_sync": true,
  "device_sync": {"renc-data-sw": "in-sync", "uky-data-sw": "in-sync"},
  "system_status": "up",
  "dataplane_status": "not_checked",
  "status": "up"
}
```

When status is **unknown**, also check:

- `sync_error` — `check_service_sync` failed (mode=`check`)
- `sync_raw` — unparsed sync payload for debugging
- `system_status_basis` — e.g. `endpoint_fleet_sync` when mode=`skip`

Inspect with:

```bash
nso-summary-run --skip-llm --dry-run 2>/dev/null | python3 -c "
import json, sys
s = json.load(sys.stdin)['snapshot']
for k, v in sorted(s['services'].items()):
    print(k, v.get('status'), v.get('in_sync'), v.get('sync_error'))
"
```

### Not included yet

These are on the [roadmap](WORK_PLAN.md) or only partly used for service up/down today (topology design: [agent/topology/docs/DESIGN.md](agent/topology/docs/DESIGN.md)):

- Broader live ops via dedicated MCP helpers (e.g. `get_live_status`) where not already covered by topology `exec_show` paths
- Ping / reachability checks beyond device sync errors from `get_fleet_sync_summary` (and any optional investigate deep-checks)

**Already implemented:** IS-IS and BGP **bidirectional** validation in `agent/topology/` (underlay + routing layers) — asymmetric/down sessions become operational status + `topology.operational.issues`, and show under Devices. Service instance sync status is separate from dataplane digs; see [Network topology](#network-topology) below.

Types listed in `IGNORE_SERVICE_TYPES` (default: `idipa`) are skipped entirely — no health checks or counts.

## Network topology

Layered topology is **implemented** for **physical**, **underlay (IS-IS)**, **routing (BGP)**, and **services** (endpoint pairs from collect). Each run embeds `topology.static` + `topology.operational` in the snapshot; canonical static file: `state/topology.static.json`.

**Bidirectional checks:** IS-IS adjacencies and BGP sessions are paired both ways (A↔B). Asymmetric or down links show as operational status (`up` / `down` / `unidirectional` or `degraded`) and as entries in `topology.operational.issues`, and in the Devices report exceptions.

Documentation:

| Doc | Purpose |
|-----|---------|
| [docs/NOTES-2026-07-13.md](docs/NOTES-2026-07-13.md) | Changelog notes for 2026-07-13 Devices/topology work |
| [docs/NOTES-2026-07-28.md](docs/NOTES-2026-07-28.md) | Services topology layer (Phase 3) |
| [docs/FAQ.md](docs/FAQ.md) | Operator FAQ (counts vs CLI, unknown, unmapped peers, services edges, …) |
| [agent/topology/README.md](agent/topology/README.md) | Package overview |
| [agent/topology/docs/DESIGN.md](agent/topology/docs/DESIGN.md) | Full design: JSON shape, MCP mapping, static vs operational |

**Pitfall:** IOS-XR **config** uses long interface names (`HundredGigE…`) while **`show`** returns abbreviations (`Hu…`). Matching uses `agent/topology/interfaces.py`. Interface **status** comes from `show interfaces brief` (not `interfaces summary` on these XR boxes).

- **Static** — config-derived graph; `state/topology.static.json`; rebuilt on config change or force update
- **Operational** — live status every run inside the snapshot (no separate `topology.operational.json`)
- **Devices report** — rollups + exceptions; BGP peers / IS-IS adjacencies count NSO-mapped neighbors and list **unmapped** CLI neighbors

### Viewing topology

Read `state/latest.json` → `topology`, or `state/runs/<run-id>/snapshot.json` / `report.md`. Details: [DESIGN.md — Viewing topology](agent/topology/docs/DESIGN.md#viewing-topology).

| Goal | Where |
|------|--------|
| Read summary (issues, layers) | `state/runs/<run-id>/report.md` — path in `state/latest.meta.json` |
| Latest static + operational JSON | `state/latest.json` → `.topology` |
| Config-only graph | `state/topology.static.json` |
| Historical run | `state/runs/<run-id>/snapshot.json` → `.topology` |
| Static graph image (Graphviz) | `scripts/export_topology_dot.py` → `.dot` / `.png` / `.svg` |

```bash
cd /Users/dec2023/Work/ESnet/NSO_summary_agent

# Latest report path
cat state/latest.meta.json

# Quick topology summary from last run
python3 -c "
import json
t = json.load(open('state/latest.json'))['topology']
print('static_source:', t.get('static_source'))
issues = t.get('operational', {}).get('issues', [])
print('issues:', len(issues))
for layer in ('underlay', 'routing', 'services'):
    s = t.get('operational', {}).get('layers', {}).get(layer, {}).get('summary', {})
    if s: print(layer, s)
"

# Pretty-print configured graph
python3 -m json.tool state/topology.static.json | less
```

#### Graphviz export (`scripts/export_topology_dot.py`)

Renders device↔device links from static topology (default layers: **underlay** + **routing**). Physical inventory edges usually have `remote: null` and are omitted unless you ask for them.

Install Graphviz once (for rendering images):

```bash
# macOS
brew install graphviz
# Debian/Ubuntu
# sudo apt-get install graphviz
```

**Option A — script writes DOT and runs `dot` for you**

```bash
# PNG (also writes topo.dot)
python3 scripts/export_topology_dot.py -o topo.dot --render png

# SVG or PDF
python3 scripts/export_topology_dot.py -o topo.dot --render svg
python3 scripts/export_topology_dot.py -o topo.dot --render pdf

# Other inputs / layers
python3 scripts/export_topology_dot.py -i state/latest.json -o topo.dot --render png
python3 scripts/export_topology_dot.py --layers underlay,routing,physical --include-unlinked -o all.dot --render png
```

**Option B — script writes DOT only; you run `dot` yourself**

```bash
python3 scripts/export_topology_dot.py -o topo.dot

dot -Tpng topo.dot -o topo.png
dot -Tsvg topo.dot -o topo.svg
dot -Tpdf topo.dot -o topo.pdf

# Preview on macOS
open topo.png
```

`--render` on the script is equivalent to calling `dot -T<format>` on the same `.dot` file.

There is **no interactive map UI in v1** — use JSON, `report.md`, or the Graphviz export above. For live NSO exploration today, use Cursor + the Cisco NSO MCP server.

## Multi-agent CLI

Sibling pipeline for IS-IS + BGP + device investigation: **`nso-multi-agent-run`**.

- Docs: [`multi_agent/README.md`](multi_agent/README.md)
- Design: [`docs/superpowers/specs/2026-08-10-multi-agent-production-mvp-design.md`](docs/superpowers/specs/2026-08-10-multi-agent-production-mvp-design.md)
- Own state under `state/multi_agent/` (does not overwrite summary-run `state/latest.json`)
- Default is dry-run; use `--publish` to Slack/email and persist artifacts

```bash
nso-multi-agent-run --spine-only
nso-multi-agent-run --publish
```

## Diagnostic MAS CLI

Blackboard coordinator with ISIS / BGP / Service / Device roles: **`nso-diagnostic-run`**.

- Design: [`docs/superpowers/specs/2026-08-17-diagnostic-mas-design.md`](docs/superpowers/specs/2026-08-17-diagnostic-mas-design.md)
- Evidence / diagnoses only from MCP + attributed conclusions; state under `state/diagnostic_mas/` (skipped in dry-run)
- Default is dry-run; use `--publish` (or `DRY_RUN=0`) for Slack/email + artifacts
- Does not replace `nso-multi-agent-run`

### Operator report (default)

Stdout / `report.md` use a concise operator layout (not the older Issues/Budget dump):

1. **Header** — Scope (devices · services), Duration, Result  
2. **Summary** — LLM prose only (when LLM is enabled); omitted on `--skip-llm`  
3. **Devices** — per-device sync / health / BGP·IS-IS / Attention (deterministic)  
4. **Services** — category table (Total / SystemUp / down / degraded / unknown);
   under `NSO_SERVICE_SYNC_MODE=skip`, a note that SystemUp starts from endpoint
   fleet sync (incomplete digs keep SystemUp; dig-confirmed down/degraded
   demote); DataplaneDig line; incomplete collection checks grouped by
   endpoint/reason; per-instance sections only for digs / confirmed impairments
   (`--services-detail` for every instance). Dig `dataplane=up` renders as
   "Passed PE-side readiness checks. Customer traffic delivery was not
   tested." (no idle-customer / outside-scope inference).  
5. **Recommended follow-up** — one action per device (overlapping involvement
   counts; do not sum), then reassess distinct unknowns; endpoint combinations
   stay under Services  
6. **Run details** — evidence counts, budgets, dry-run / reporting notes  

`--full` appends **Appendix: Detailed Device Analysis** (legacy per-device dump).

**Publish:** Slack gets mrkdwn blocks; email gets HTML + plain (`agent/markdown_channels.py`). Raw markdown is not left unrendered on those channels.

### Useful flags

| Flag | Role |
|------|------|
| `--skip-llm` | Spines only; no dataplane/drill/summary LLM |
| `--deterministic-summary` | Skip final Summary chat; restate dataplane diagnoses/findings (default is LLM Summary) |
| `--full` | Appendix detailed devices |
| `--service-type` / `--service-id` | **Service-first** (implies lean service-only): match instances, basic checks for all; with LLM enabled, an explicit `--service-id` also investigates instances whose basic checks pass (within budget); type-only focus investigates suspicious instances; no whole-NSO topology unless evidence expands. Combined = intersection; exit if nothing matches |
| `--max-dataplane-tools N` | Per-service dataplane verify budget (default 40; `0` skips) |
| `--max-dataplane-services N` | Total cap on dataplane LLM instances (default without per-category: one best per typed prompt; **two** if only one typed category is present) |
| `--max-dataplane-per-category N` | Up to N digs from each typed prompt category (even overnight sample; optional total cap still applies) |
| `--max-drill-issues N` | Run-wide drill slot count (default 2; separate from dataplane selection) |
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

# Operator report + detailed device appendix
nso-diagnostic-run --full --dry-run

# Compare two published runs (new / recovered / persistent / coverage)
nso-diagnostic-delta state/diagnostic_mas/runs/<older> state/diagnostic_mas/runs/<newer>
```

## Project layout

```
nso_facts/        # MCP, topology, health, delta, metrics, fact_pack (no LLM)
nso_report/       # deterministic executive/device formatters (no LLM)
agent/            # CLI, Fabric summarize, publish, config (+ shims to facts/report)
  topology/       # shim → nso_facts.topology
multi_agent/      # nso-multi-agent-run (IS-IS/BGP/device workers)
diagnostic_mas/   # nso-diagnostic-run (operator report + dataplane/drill)
experiments/
  multi_agent/    # deprecation shim → multi_agent.run
docs/
  TRY_ME_OUT.md   # install + common ways to run
  DOCKER.md       # build/run via Docker
  FAQ.md          # operator FAQ (Devices / topology counts)
  NOTES-*.md      # dated work notes
state/            # gitignored — snapshots; topology.static.json
deploy/
  com.esnet.nso-summary.plist
  monitoring/     # docker compose + prometheus.yml + grafana dashboards
WORK_PLAN.md
```

## Next steps (from work plan)

- Prometheus metrics export from `nso-summary-run` (**Phase 1 done** — set `PROMETHEUS_PUSHGATEWAY_URL`)
- Services topology layer
- Grafana dashboards for service / topology trends (starter dashboard in `deploy/monitoring/grafana/`)



### Diagnostic fault history and follow-up

Diagnostic reports prioritize current service faults and unresolved last-known
faults before collection gaps and inventory observations. A new down/degraded
finding is labeled **newly detected**, not a proven regression: an earlier
readiness pass may have used different checks.

Published diagnostic cases persist `last_known_service_faults` with their last
observation run and explanation. Sampling omissions, absent inventory, baseline
sync passes, and inconclusive rechecks do not clear those findings. A completed
current dataplane pass clears the historical fault; current faults refresh it.
Historical findings are reported separately and do not alter current-run service
counts or masquerade as fresh evidence. Old cases without the field are supported.
Dry-runs can show history from the last published case but do not persist updates.


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
report. Email includes the self-contained HTML as an attachment: download it
and open it in a browser for search, service-status filters, expandable details,
and section navigation. No additional LLM calls are used. Terminal Markdown and
legacy summary/multi-agent publishing remain unchanged. Dry runs do not publish
or save the HTML report.

Slack file attachments require `SLACK_BOT_TOKEN` with `files:write` and
`SLACK_CHANNEL_ID`; invite the bot to that channel. The upload uses Slack's
getUploadURLExternal → upload → completeUploadExternal flow. Existing webhook-only
setups still receive a compact digest, but cannot attach the HTML. Optionally set
`DIAGNOSTIC_REPORT_BASE_URL` to an existing internal HTTPS location serving the
contents of the diagnostic `runs/` directory; webhook posts will link to
`<base>/<run-id>/report.html`. The runner does not deploy or host those files.
Without bot upload or hosting, full details remain in run artifacts and the email
attachment. HTML attachments may require downloading or may be blocked by mail policy.
