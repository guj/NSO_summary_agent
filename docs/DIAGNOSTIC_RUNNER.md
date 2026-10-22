# Diagnostic runner

## New machine, step by step

For Linux or macOS with nothing installed but Python and, ideally, git. You need a
GitHub account that can read the fabric-testbed repositories (the MCP server's is
private), network access to GitHub and PyPI while installing, HTTPS access to your NSO
server (through a VPN if your site requires one), and an NSO account. An
OpenAI-compatible LLM endpoint and key are optional.

### 1. Get this repository

With git:

```sh
git clone -b <branch> <URL of this repository> nso-agent
```

For a private repository, git needs your GitHub sign-in: over HTTPS, your username and a
personal access token as the password (or one already stored by a credential helper or
`gh auth login`); over SSH (`git@github.com:…`), a key added to your GitHub account. On a
Mac without the developer tools, `git --version` offers to install them.

Without git: use **Code → Download ZIP** on this repository's GitHub page, signed in, on
the branch you are reading, unpack it (for example `python3 -m zipfile -e <file>.zip .`),
and rename the folder to `nso-agent`. Step 2 then needs a token, as described there.

### 2. Install

```sh
cd nso-agent
python3 install.py
```

It needs Python 3.12 or newer. If `python3` is older, it looks for `python3.12` or newer
on your PATH and uses that. If there is none, it stops and says how to install one: the
installer from <https://www.python.org/downloads/> on macOS,
`sudo apt install python3.12 python3.12-venv` on Ubuntu or Debian, or
`sudo dnf install python3.12` on RHEL, Rocky or Alma 9.

It then:

- puts the NSO MCP server into `mcp-server/`, at the version this agent was tested with
  (`--mcp-ref main` takes the latest instead). With git, it clones it the way you cloned
  this repository, SSH or HTTPS, with the same sign-in. Without git, or when that clone
  fails, it downloads it through GitHub's API with a personal access token that can read
  `fabric-testbed/fabric-nso-mcp-server` (a classic token needs the `repo` scope):
  `GITHUB_TOKEN=<token> python3 install.py`. The token is used for that download only and
  is not saved;
- creates `.venv` and installs the MCP server and the agent into it;
- copies `.env.example` to `.env`, only if `.env` does not exist yet;
- asks for your settings and writes them into `.env` (step 3).

It does not start Pushgateway, Prometheus or Grafana. They are optional and run in
Docker: for the Grafana dashboards, start them with `docker compose up -d` in
`deploy/monitoring`, as described in [deploy/monitoring/README.md](../deploy/monitoring/README.md),
and answer yes to the Pushgateway question. Scans run and report the same without them.

Running it again is safe: it keeps your `.env`, and changes the MCP server only for a
different version. Do not `pip install cisco-nso-mcp-server` from PyPI instead:
that is Cisco's upstream package, without the FABRIC tools this agent uses.

### 3. Answer the setting questions

At the end of the install, the agent asks for its settings and writes them into `.env`:

| Section | Asked | What it asks |
|---|---|---|
| NSO | always | hostname (one that its HTTPS certificate names), username, password, whether to skip certificate verification (`NSO_VERIFY=0`, as for a self-signed certificate), and if not, a CA bundle path if NSO's certificate is from a private CA |
| LLM | if you want one | base URL, API key, model id, temperature |
| Slack | if you want reports there | a bot token and channel ID to upload the HTML report, or a webhook URL for a one-line notice |
| Email | if you want reports by email | SMTP host and port, login if needed, from and to addresses |
| Pushgateway | if you run Grafana | the Pushgateway URL (default `http://127.0.0.1:9091`); the stack itself you start as in [deploy/monitoring/README.md](../deploy/monitoring/README.md) |

Press Enter to keep the value shown in brackets. Passwords, keys and tokens are typed
without echo and shown only as "(set)". A section you answer "no" to is left as it was.
Without an LLM, add `--skip-llm` to every scan. At the end it offers to run the
connection check.

To change settings later, run the same questions again from the checkout root:

```sh
.venv/bin/nso-diagnostic-run --configure
```

You can also edit `.env` directly; keep it private. `MCP_SERVER_CMD` is not needed: the
agent uses the MCP server installed next to it in `.venv`. If a required setting is
missing, a scan stops with one message naming each of them.

### 4. Check, then scan

```sh
.venv/bin/nso-diagnostic-run --check-connection
.venv/bin/nso-diagnostic-run --skip-llm --dry-run
```

`--check-connection` starts the MCP server and asks NSO for its device list, and checks
the LLM key and delivery settings when they are set. The second command runs a full scan
without the LLM and prints the report; nothing is saved or sent. The sections below cover
LLM use, publishing to Slack and email, and scheduling.

### Using your own MCP server installation

Set `MCP_SERVER_CMD` in `.env` to the absolute path of its `cisco-nso-mcp-server`
command. It must be fabric-nso-mcp-server's `main` from 21 October 2026 (pull request
#11) or later, which provides the `get_bgp_summary` tool the agent reads BGP state with.

## Manual installation

The same result as `install.py`, step by step, with git. Both projects need Python 3.12
or newer: if `python3 --version` shows an older one, use `python3.12` (or newer) in the
first line, installed as in [step 2](#2-install). From the checkout root:

```sh
python3 -m venv .venv
git clone https://github.com/fabric-testbed/fabric-nso-mcp-server.git ../fabric-nso-mcp-server
.venv/bin/python -m pip install ../fabric-nso-mcp-server
.venv/bin/python -m pip install -e .
.venv/bin/nso-diagnostic-run --configure
.venv/bin/nso-diagnostic-run --check-connection
```

The [FABRIC NSO MCP server](https://github.com/fabric-testbed/fabric-nso-mcp-server)
repository is private, so the clone needs your GitHub sign-in, as in
[step 1](#1-get-this-repository). Its `main` from 21 October 2026 (pull request #11) or
later works. To match the version `install.py` installs, run
`git -C ../fabric-nso-mcp-server checkout <commit>` before installing it, with the commit
named `MCP_REF` in `install.py`. Clone it outside this checkout: `mcp-server/` belongs to
`install.py`. To update it later, `git -C ../fabric-nso-mcp-server pull` and run its
`pip install` line again.

Installed into the same `.venv`, the MCP server is found without `MCP_SERVER_CMD`. Its
own README installs it with `uv` into its own environment instead; that also works, with
`MCP_SERVER_CMD` set as in
[Using your own MCP server installation](#using-your-own-mcp-server-installation). Its
`uvx` example runs Cisco's package from PyPI, which lacks the FABRIC tools.

`--configure` creates `.env` from `.env.example` and asks for the settings, as in
[step 3](#3-answer-the-setting-questions); you can also copy `.env.example` to `.env`
and edit it. No credentials, virtual environments or previous run state are shipped.
Run from the checkout root so local configuration is unambiguous. Use
`nso-diagnostic-run` directly; concurrency is built into this command.

LLM settings are `FABRIC_AI_API_KEY`, `FABRIC_AI_API_URL`, and `FABRIC_AI_MODEL`.
Use an OpenAI-compatible chat-completions endpoint, not an Anthropic-only endpoint.
Temperature defaults to 0.1; set `FABRIC_AI_TEMPERATURE=1` if the model requires it,
or leave the value explicitly blank to omit the parameter.
`FABRIC_CHAT_TIMEOUT_SEC` defaults to 60 seconds per attempt,
`FABRIC_CHAT_CONNECT_TIMEOUT_SEC` to 20, and `FABRIC_CHAT_MAX_RETRIES` to 0.
These are request limits, not an overall scan deadline.

## Editable versus regular installation

The setup above uses an **editable installation** (`-e`). Choose the mode based
on how you use the checkout:

- **Editable:** `python -m pip install -e /path/to/checkout` points the installed
  command at that checkout. Most Python and prompt/template edits take effect on
  the next run without reinstalling. Keep the checkout in place. Reinstall when
  dependencies, entry points, or packaging configuration change.
- **Regular:** `python -m pip install /path/to/checkout` builds and copies the
  package into the environment's `site-packages`. Later checkout edits do not
  update that copy. Reinstall after source updates. This mode is useful for
  testing what a fresh installation receives, including packaged HTML assets.

Activate the environment used for your scans, then check it:

```sh
command -v nso-diagnostic-run
python -m pip list --editable
python -m pip show nso-summary-agent
```

An editable installation appears in the first pip command with its source path;
`pip show` includes **Editable project location**. A regular installation has no
such field. If the package is absent, check that you selected the correct Python
environment. Use `/path/to/venv/bin/python -m pip ...` to target one explicitly;
a bare `pip` command may belong to a different environment.

Refresh a regular installation from the updated checkout:

```sh
/path/to/venv/bin/python -m pip install --upgrade --force-reinstall --no-deps /path/to/checkout
```

This replaces the installed package without reinstalling dependencies. If the
update changes dependencies, omit `--no-deps`. It does not edit your `.env`,
regenerate existing reports, or change a scan already running. Start a new process
for the updated code to take effect.

To switch that environment to editable mode instead:

```sh
/path/to/venv/bin/python -m pip install -e /path/to/checkout
```

Running `python -m diagnostic_mas.run` from the checkout root can also load local
source directly. That behavior alone does not prove an editable installation.
For an unambiguous installed-module check, excluding the current directory:

```sh
/path/to/venv/bin/python -I -c "import diagnostic_mas.run as r; print(r.__file__)"
```

The path identifies which code that environment loads. Use the same environment's
`nso-diagnostic-run` command for the scan. Updating a checkout and updating a
regular installed copy are separate steps.

## Fresh-client configuration checklist

Edit `.env` before a live run. Do not commit it.

- **NSO access:** set `NSO_ADDRESS` to your NSO hostname or IP, `NSO_USERNAME` to your
  account, and `NSO_PASSWORD` to its password. HTTPS port 443 is the default. Set
  `MCP_SERVER_CMD` only for an MCP server installed elsewhere; the one `install.py`
  puts in `.venv` is found on its own.
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
  Each channel is delivered on its own: if Slack fails, email is still sent and the reports
  are still saved. The run then prints a `Delivery failed:` line naming the channel and
  ends with a non-zero exit status.

### Slack: choose notification or HTML attachment

`SLACK_WEBHOOK_URL` sends a short notification. It cannot upload the HTML file.

To upload the HTML report to Slack, use a bot instead:

1. Create a Slack app for your workspace, give it the bot scope `files:write`, install it,
   and copy its bot token (it starts with `xoxb-`) into `SLACK_BOT_TOKEN`. Optionally add
   `channels:read` (`groups:read` for a private channel) so that `--check-connection` can
   confirm the bot is in the channel.
2. Put the destination channel's ID (for example `C0123ABCD`, not the channel name) into
   `SLACK_CHANNEL_ID`.
3. Invite the bot to that channel with `/invite @your-app-name`. **The bot must be a member
   of the channel**; otherwise the upload fails with `not_in_channel`.

Both settings are required. When both are set they take precedence over the webhook for
reports with attachments; with only one of them, the webhook is used. `DIAGNOSTIC_REPORT_BASE_URL` optionally adds a hosted
report link; it does not upload or host files itself.

### Email: SMTP host is required

To enable email, set **all three** of `SMTP_HOST`, `EMAIL_FROM`, and `EMAIL_TO`.
The HTML report is attached. `EMAIL_TO` accepts several recipients, separated by commas or semicolons.

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

## Running without an LLM and choosing a model

An LLM is optional. To collect NSO/device facts, run deterministic operational
checks, and produce a report without model calls:

```sh
nso-diagnostic-run --skip-llm --dry-run
```

No LLM API key is required in this mode. NSO/MCP access is still required; this is
a live scan, not an offline simulation. LLM investigations, drills, and generated
summaries are skipped. Confirmed operational faults remain faults; unresolved
checks remain Unknown rather than being assumed healthy. Remove `--dry-run` and
configure publication separately if you want saved/delivered reports.

For LLM-assisted diagnosis, users can use an institution-provided or free-access
model endpoint, such as the FABRIC/NRP endpoints used during development, or their
own paid provider. Free access depends on provider eligibility, quotas, and current
availability; the agent itself does not provide free model access. The endpoint
must support the runner's OpenAI-compatible chat-completions interface. Set
`FABRIC_AI_API_KEY`, `FABRIC_AI_API_URL`, and `FABRIC_AI_MODEL` to your provider's
values; the FABRIC prefix does not lock the agent to one provider.

**Experience from our development scans:** the endpoint model named
`claude-opus-5` performed better overall in our trials, diagnosing service issues
in reasonable time. Other tested models sometimes took substantially longer,
timed out, or produced incomplete or incorrect diagnoses. These are observations
from our network, prompts, and provider endpoints—not a controlled benchmark or
a guarantee. Provider load, model capability, context size, tool latency, and
service complexity all affect results. A timeout is not evidence of a service outage.

Use the exact model ID advertised by your endpoint. The tested LiteLLM Opus 5
endpoint required `FABRIC_AI_TEMPERATURE=1`; this is provider/model-specific.
Compare candidate models on the same selected services and evidence, checking
correctness as well as elapsed time and tool use. A completed response alone is
not a verified diagnosis. Review the effective model, temperature, and request
limits in the report's configuration section.

## Running

```sh
# Check the MCP server, NSO login, LLM key and delivery settings before a scan
nso-diagnostic-run --check-connection
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
persistence and configured delivery.
`--save-mcp-results` opts into raw MCP evidence retention; outputs can contain
sensitive device configuration, so do not commit them. Such a run first removes
archives older than `MCP_RESULTS_KEEP_DAYS` (default 7 days; `0` keeps all).

`--check-connection` starts the MCP server, asks NSO for its device list and, unless
`--skip-llm` is given, checks the LLM key and model with a one-token request. When the
provider reports spend, it also prints the LLM budget that is left. It prints one
line per check and exits with a non-zero status if any check fails; nothing is scanned,
saved or published. A blank `FABRIC_AI_API_KEY` is reported as not configured, not as a
failure.

It also checks the delivery channels you have configured, without sending anything: the
Slack bot token (and, if the bot also has the `channels:read` scope, that the bot is in the
channel), and the SMTP connection and login for email. A Slack webhook cannot be verified
without posting to it, so it is only reported as configured.

### When NSO stops answering during a scan

When a query times out, or the connection to NSO fails, the runner asks NSO once for its
device list, a request that contacts no device. If NSO answers, a timeout is recorded
against the device that was queried, and that device is skipped for the rest of the run. If
NSO does not answer, for example because the VPN dropped, the scan stops at once with
`NSO is unreachable; scan stopped` and exit status 3. No report is printed, saved or
published, and nothing is retried: run the scan again when NSO is reachable.

### Example: our usual full scan with logging and publication

This is the command used for our larger scans, not the default configuration or
an optimal setting for every NSO deployment. Run it in Bash or Zsh after activating
your environment and configuring NSO, LLM, and publication settings:

```bash
mkdir -p log
{ time nso-diagnostic-run \
    --spine-concurrent-devices 6 \
    --dataplane-concurrent_works 2 \
    --max-dataplane-per-category 30 \
    --max-dataplane-tools 40 \
    --max-drill-issues 2 \
    --publish; } > "log/test.txt" 2>&1
```

- `--spine-concurrent-devices 6`: up to six concurrent operational device calls,
  with per-device locking. This does not parallelize every spine stage.
- `--dataplane-concurrent_works 2`: up to two service digs at once, subject to
  device-conflict scheduling.
- `--max-dataplane-per-category 30`: select up to 30 eligible services per service
  type for dataplane investigation. Operational passes still skip routine digs;
  this does not request 30 investigations if fewer services are eligible.
- `--max-dataplane-tools 40`: at most 40 MCP tool calls per dataplane dig, not 40
  calls for the whole scan.
- `--max-drill-issues 2`: up to two issue-drill investigations, separate from the
  service dataplane selection and its tool budget.
- `--max-port-investigations` (default 6, not shown above): a down port that Down services
  attach to, or a device cut off from the network, is investigated once before the services,
  and the services it explains are then not investigated individually. `0` turns this off.
- `--publish`: save the case and reports and deliver through configured channels.
- `{ time ...; } > "log/test.txt" 2>&1`: capture standard output, standard error,
  and shell timing in one file. `>` overwrites an existing `log/test.txt`; use a
  different filename for each scan if you want to retain earlier logs. The HTML
  report is saved separately under the configured state directory.

Watch progress from another terminal with `tail -f log/test.txt`. Logs may contain
network details; keep them outside version control. These limits are ceilings,
not targets, and higher concurrency does not guarantee proportional speedup.

## Claude Code commands

The repository ships five slash commands in `.claude/commands/`. Each one runs
`nso-diagnostic-run` with the matching flags and has Claude Code summarize the output.

| Command | What it runs | Arguments |
|---|---|---|
| `/nso-report` | Full diagnostic report | `[llm] [publish] [max-…=N …] [device1,device2]` |
| `/nso-service` | Services only, filtered by type, ID and/or device | `[llm] [l3rt\|type=…] [id=…] [device…]` |
| `/nso-device` | One or more devices: sync and hardware only; a device name is required | `[llm] device\|device1,device2` |
| `/nso-bgp` | BGP only | `[llm] [device\|device1,device2]` |
| `/nso-isis` | IS-IS only | `[llm] [device\|device1,device2]` |

All five run without the agent's own LLM (`--skip-llm`) unless you add the word
`llm`; Claude Code then summarizes the fixed-check output itself. Only `/nso-report`
can publish, and only with the word `publish`; otherwise it runs with `--dry-run`.
The other four never publish. A dry run still contacts NSO and the devices, and with
`llm` it also uses the configured LLM endpoint and its budget.

## Workflow and concurrency

1. Collect inventory and endpoint fleet sync. Inconclusive sync is retried once
   per relevant endpoint; unresolved sync remains Unknown and skips operational
   checks and routine service digs. A collection error is not out-of-sync.
   The collection phase ends by reading the error counters of the ports under
   services and IS-IS adjacencies, for comparison with the next scan
   (`--max-port-counter-reads`, default 400; 0 turns it off).
2. Run deterministic operational checks using NSO intent to identify exact
   service interfaces and objects. L2PTP checks endpoint xconnects and follows the
   service label in each direction; L2Bridge
   checks local bridge membership/forwarding; L2STS additionally checks effective
   EVPN RTs, replication and transport; L3RT checks gateway, routes and forwarding
   including configured border dependencies.
   A fault the checks show in full (an L3RT gateway address held by another
   service on the same device) is reported Down with no LLM investigation.
3. An LLM Up needs an operational-check reading from every device of the service;
   without one the service stays Unknown and the finding is kept as a note.
   Operational passes skip routine LLM investigation. Other eligible services
   are investigated within category/total caps. An exact service-ID request can
   force a dig. Incomplete LLM work does not erase a confirmed operational fault.
4. Report final OpUp/Down/Degraded/Unknown and verification gaps. OpUp establishes
   PE-side readiness, not customer traffic delivery. Disappeared services are
   freshly rechecked and excluded from active counts when absence is confirmed.
   Each LLM investigation starts with a fresh, uncached service listing; a service
   its owner deleted after the scan began is reported as no longer present and is
   not investigated. During an investigation the service list is always read fresh.

`--spine-concurrent-devices` and `--dataplane-concurrent_works` both default to 1.
The former bounds concurrent operational device calls, with per-device locking
and shared run-local collection caches; it does not parallelize every spine stage.
The latter schedules service digs with disjoint reserved devices. CDB-only reads
can proceed concurrently; extra-device conflicts are explicit verification gaps.
Each dig has isolated conversation, evidence and budgets, merged by the coordinator.

`--max-dataplane-tools` defaults to 40 per dig. `--max-dataplane-services N` caps
how many services get a dig per scan, whatever their type (default 10; 0 turns
digs off), taking eligible services in turns across types, most suspicious first.
`--max-dataplane-per-category N` also limits each service type. `--max-deep-checks` and `--max-handoffs` default
to zero; these older mechanisms are separate from dataplane digs and issue drills.

## Reports and topology

Saved reports live under `state/diagnostic_mas/runs/<run-id>/` by default:
`case.json`, `report.md`, and self-contained `report.html`. The HTML attachment
contains a collapsible circular routing topology with IS-IS/BGP toggles, curved
parallel links, device selection and endpoint evidence. No extra queries or
external assets are needed. Open HTML in a browser; mail clients may disable its
scripts in attachment previews. Each device entry also has a collapsed **Interfaces and
hardware** block with interface counts, hardware readings and exceptions; this detail
appears only in the HTML report.

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

The tests need the optional `dev` extra (pytest). It is not required to run the
agent. Keep the quotes; zsh otherwise treats the brackets as a pattern.

```sh
python -m pip install -e '.[dev]'
python -m pytest tests/test_topology_report.py tests/test_bgp_partial_evidence.py tests/test_spine_concurrency.py tests/test_dataplane_scheduler.py
python -m pytest
```

Do not run a live scan as an installation smoke test; once credentials are configured, use
`--check-connection`. `--help`, imports and the
offline tests are sufficient without NSO credentials. Monitoring and container
instructions remain in `deploy/monitoring/README.md` and `docs/DOCKER.md`.

## Service topology in HTML reports

The collapsed **Service topology** section uses saved case evidence; it adds no MCP or LLM calls. Select a device, service type, final status, or search by service ID/attachment. Six services are shown per page, with faults and verification gaps first. Click a service to see exact recorded attachments, configured border dependencies, endpoint sync, basic checks, and separately attributed investigation findings.

Colors represent each service’s final assessment using the same logic as the summary table; they do not diagnose each drawn link. Connections represent logical membership, not physical cables. OpUp means PE-side readiness, not tested customer delivery. Missing evidence remains unknown, and services confirmed absent during the run are excluded. Raw configuration dumps are not embedded. Reports rendered without a saved case omit the diagram. The self-contained HTML also works as an attachment without external scripts.
