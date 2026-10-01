# Diagnostic runner

## Independent checkout

Use Python 3.12 or newer. From the checkout root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
nso-diagnostic-run --help
```

Install the [Cisco NSO MCP server](https://github.com/fabric-testbed/fabric-nso-mcp-server)
separately, following its installation instructions. Set `MCP_SERVER_CMD` to its
executable (absolute path if not on PATH), and configure NSO address and credentials
in your local `.env`. No credentials, virtual environments or previous run state
are shipped. Run from the checkout root so local configuration is unambiguous.
Use `nso-diagnostic-run` directly; concurrency is built into this command.

LLM settings are `FABRIC_AI_API_KEY`, `FABRIC_AI_API_URL`, and `FABRIC_AI_MODEL`.
Use an OpenAI-compatible chat-completions endpoint, not an Anthropic-only endpoint.
Temperature defaults to 0.1; set `FABRIC_AI_TEMPERATURE=1` if the model requires it,
or leave the value explicitly blank to omit the parameter.
`FABRIC_CHAT_TIMEOUT_SEC` defaults to 60 seconds per attempt,
`FABRIC_CHAT_CONNECT_TIMEOUT_SEC` to 20, and `FABRIC_CHAT_MAX_RETRIES` to 0.
These are request limits, not an overall scan deadline.

## Fresh-client configuration checklist

Edit `.env` before a live run. Do not commit it.

- **NSO access:** set `MCP_SERVER_CMD` to the separately installed MCP executable,
  `NSO_ADDRESS` to your NSO hostname or IP, `NSO_USERNAME` to your account, and
  `NSO_PASSWORD` to its password. HTTPS port 443 is the default.
- **Certificate verification:** enabled by default (`NSO_VERIFY=1`). Use a hostname
  covered by the certificate and, for a private CA, `NSO_CA_BUNDLE`. A CA bundle
  does not fix a hostname/IP mismatch. `NSO_VERIFY=0` disables verification; use
  it only when deliberately accepting that limitation in a lab.
- **LLM:** configure `FABRIC_AI_API_KEY`, `FABRIC_AI_API_URL` and `FABRIC_AI_MODEL`.
  The URL is the provider base URL (host root or `/v1`), not the full
  `/chat/completions` path. Models requiring temperature 1 need
  `FABRIC_AI_TEMPERATURE=1`; this is model-specific, not required for all clients.
  Use `--skip-llm` if no model is configured.
- **Service sync:** code default is `NSO_SERVICE_SYNC_MODE=check`. The example
  explicitly uses `skip` for environments where per-service sync is inconclusive
  or expensive; endpoint fleet sync, operational checks and eligible digs still run.
- **Output:** set `STATE_DIR` to your writable output directory. Prefer an absolute
  path when installing the package and running outside the checkout.
- **Delivery:** optional. Use `--publish` (or `DRY_RUN=0`) to save and deliver reports.
  Dry-run does not save normal report state or deliver notifications.

### Slack: choose notification or HTML attachment

`SLACK_WEBHOOK_URL` sends a short notification. It cannot upload the HTML file.
For diagnostic HTML uploads, set both `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID`;
the bot needs `files:write` permission and membership in the destination channel.
When both bot settings are available, they take precedence over the webhook for
reports with attachments. `DIAGNOSTIC_REPORT_BASE_URL` optionally adds a hosted
report link; it does not upload or host files itself.

### Email: SMTP host is required

To enable email, set **all three** of `SMTP_HOST`, `EMAIL_FROM`, and `EMAIL_TO`.
The HTML report is attached. `EMAIL_TO` accepts comma-separated recipients.

- `SMTP_HOST` is the mail server hostname, not an email address.
- `SMTP_PORT` defaults to 587 and `SMTP_USE_TLS` to 1 (STARTTLS); use settings
  specified by your mail administrator.
- `SMTP_USER` and `SMTP_PASSWORD` are required only when the server requires
  authentication. An authorized relay may accept mail without them.
- To disable email, remove or clear `EMAIL_TO`, including any exported value.
  Setting recipients without a host causes publication to fail.

### Running from a separate directory

Install the checkout into your own virtual environment, copy `.env.example` into
that working directory, and set an absolute `STATE_DIR`. Explicitly export the
file before invoking the installed command so configuration does not depend on
where Python finds a dotenv file:

```sh
set -a
. ./.env
set +a
nso-diagnostic-run --dry-run
```

Existing exported variables normally override `.env`; changing a file does not
replace values already exported in your shell unless you source it again.
If inventory collection fails and the report shows zero devices/services, do not
interpret a “no faults” summary as a successful scan. Correct the collection error
first. If only publication fails after collection, saved report files can be
resent without repeating the network scan.

## Running

```sh
# Live collection without LLM or publication
nso-diagnostic-run --skip-llm --dry-run
# LLM-assisted scan with conservative concurrency
nso-diagnostic-run --spine-concurrent-devices 2 --dataplane-concurrent_works 2 --dry-run
# Investigate one service (replace with its exact ID)
nso-diagnostic-run --service-only --service-id SERVICE_ID --dry-run
# Save and publish with your configured channels
nso-diagnostic-run --publish
```

Dry-run still contacts NSO and, unless skipped, the LLM. It does not save the
normal case/report state or send Slack/email. `--publish` explicitly enables
persistence and configured delivery. Use `--full` for detailed device analysis.
`--save-mcp-results` opts into raw MCP evidence retention; outputs can contain
sensitive device configuration, so do not commit them.

## Workflow and concurrency

1. Collect inventory and endpoint fleet sync. Inconclusive sync is retried once
   per relevant endpoint; unresolved sync remains Unknown and skips operational
   checks and routine service digs. A collection error is not out-of-sync.
2. Run deterministic operational checks using NSO intent to identify exact
   service interfaces and objects. L2PTP checks endpoint xconnects; L2Bridge
   checks local bridge membership/forwarding; L2STS additionally checks effective
   EVPN RTs, replication and transport; L3RT checks gateway, routes and forwarding
   including configured border dependencies.
3. Operational passes skip routine LLM investigation. Other eligible services
   are investigated within category/total caps. An exact service-ID request can
   force a dig. Incomplete LLM work does not erase a confirmed operational fault.
4. Report final OpUp/Down/Degraded/Unknown and verification gaps. OpUp establishes
   PE-side readiness, not customer traffic delivery. Disappeared services are
   freshly rechecked and excluded from active counts when absence is confirmed.

`--spine-concurrent-devices` and `--dataplane-concurrent_works` both default to 1.
The former bounds concurrent operational device calls, with per-device locking
and shared run-local collection caches; it does not parallelize every spine stage.
The latter schedules service digs with disjoint reserved devices. CDB-only reads
can proceed concurrently; extra-device conflicts are explicit verification gaps.
Each dig has isolated conversation, evidence and budgets, merged by the coordinator.

`--max-dataplane-tools` defaults to 40 per dig. `--max-dataplane-per-category N`
controls sampling per service type; `--max-dataplane-services N` caps the combined
selection. Without category selection, the default chooses one best eligible
instance per supported category. `--max-deep-checks` and `--max-handoffs` default
to zero; these older mechanisms are separate from dataplane digs and issue drills.

## Reports and topology

Saved reports live under `state/diagnostic_mas/runs/<run-id>/` by default:
`case.json`, `report.md`, and self-contained `report.html`. The HTML attachment
contains a collapsible circular routing topology with IS-IS/BGP toggles, curved
parallel links, device selection and endpoint evidence. No extra queries or
external assets are needed. Open HTML in a browser; mail clients may disable its
scripts in attachment previews.

The topology shows discovery evidence, not reconciled later drill conclusions.
Amber dashed device rings mean protocol-specific collection failed; click for
errors. Amber dashed links mean incomplete verification. Red links indicate an
observed failed/non-established state. For BGP, Established plus unavailable
remote evidence is Unknown; Idle plus unavailable remote evidence remains Down.
Missing observations alone never prove an outage. Parallel adjacencies count as
separate relationships; device-pair counts collapse those connections.

Disabled and unused deep-check/handoff counters are hidden from Run details;
limits remain in Run configuration. Historical findings, current faults and
verification gaps must be interpreted separately from recovery or regression.

## Offline validation

```sh
python -m pytest tests/test_topology_report.py tests/test_bgp_partial_evidence.py tests/test_spine_concurrency.py tests/test_dataplane_scheduler.py
python -m pytest
```

Do not run a live scan as an installation smoke test. `--help`, imports and the
offline tests are sufficient without NSO credentials. Monitoring and container
instructions remain in `deploy/monitoring/README.md` and `docs/DOCKER.md`.

## Service topology in HTML reports

The collapsed **Service topology** section uses saved case evidence; it adds no MCP or LLM calls. Select a device, service type, final status, or search by service ID/attachment. Six services are shown per page, with faults and verification gaps first. Click a service to see exact recorded attachments, configured border dependencies, endpoint sync, basic checks, and separately attributed investigation findings.

Colors represent each service’s final assessment using the same logic as the summary table; they do not diagnose each drawn link. Connections represent logical membership, not physical cables. OpUp means PE-side readiness, not tested customer delivery. Missing evidence remains unknown, and services confirmed absent during the run are excluded. Raw configuration dumps are not embedded. Reports rendered without a saved case omit the diagram. The self-contained HTML also works as an attachment without external scripts.
