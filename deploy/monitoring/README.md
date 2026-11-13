# Prometheus + Grafana (local monitoring stack)

Optional **Pushgateway → Prometheus → Grafana** stack for the diagnostic runner's time-series metrics. The NSO agent runs on the **host** (cron/launchd); this compose file runs the metrics backends in Docker.

```
nso-diagnostic-run (host)  --POST-->  Pushgateway :9091
                                         ^
Prometheus :9090  ---------------- scrape
       |
Grafana :3000  ----- queries Prometheus
```

Agent metrics push is **enabled** when `PROMETHEUS_PUSHGATEWAY_URL` is set in the agent `.env`. Use the smoke test below to validate the pipeline without a full agent run.

## Prerequisites

- Docker (Docker Desktop or OrbStack)
- Ports **9090**, **9091**, and **3000** free on the host

If you already run containers named `prometheus` or `pushgateway` on those ports, stop them first or use only the config files from this directory with your existing stack (see [Using an existing Prometheus](#using-an-existing-prometheus)).

## Quick start

```bash
cd /path/to/checkout/deploy/monitoring

# If old containers use the same ports:
# docker stop prometheus pushgateway 2>/dev/null; docker rm prometheus pushgateway 2>/dev/null

docker compose up -d
docker compose ps
```

| Service | URL | Default login |
|---------|-----|----------------|
| Pushgateway | http://localhost:9091 | — |
| Prometheus | http://localhost:9090 | — |
| Grafana | http://localhost:3000 | `admin` / `admin` |

Grafana provisions the **Prometheus** datasource and loads two dashboards in folder **NSO**: **NSO Diagnostic**, the scan results, and **NSO Summary**, a Pushgateway smoke test.

## Which folder the stack reads from

The stack reads its configuration from the folder it was started in. `docker-compose.yml` shares `./grafana/provisioning/datasources`, `./grafana/provisioning/dashboards`, `./grafana/dashboards` and `./prometheus/prometheus.yml` into the containers, read-only (and `./grafana/alerting` when the [scan alert](#alert-when-scans-stop) is on), and `./` is the folder that holds the compose file you started. Docker Compose also reads an optional `.env` from that folder; only the scan alert uses it.

Grafana rescans the dashboards folder about every 10 seconds, so a dashboard file added there appears without a restart. A file added to a different checkout does not appear.

To see which folder a running stack uses:

```bash
docker inspect nso-grafana --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
```

To switch to another checkout, stop the stack where it was started and start it in the new one:

```bash
docker compose -f /path/to/old/checkout/deploy/monitoring/docker-compose.yml down
docker compose -f /path/to/new/checkout/deploy/monitoring/docker-compose.yml up -d
```

Prometheus history, Grafana settings and the values last pushed to the Pushgateway are kept across the switch and across restarts: they live in Docker volumes named after the compose project (`nso-monitoring`), not in the folder. The Pushgateway saves its values every 5 minutes and when it stops, so only a crash soon after a scan loses that scan's values until the next one. Do not add `-v` to `down`; that deletes the volumes.

## Connect Grafana to Prometheus

With **`docker compose up -d`** from this directory, connection is **automatic**:

- Grafana container reaches Prometheus at `http://prometheus:9090` (see `grafana/provisioning/datasources/prometheus.yml`)
- No manual setup if you use container **`nso-grafana`**

**Verify the connection**

1. Open http://localhost:3000 → log in (`admin` / `admin`)
2. **☰ menu → Connections → Data sources → Prometheus**
3. Click **Save & test** at the bottom → must show **“Data source is working”**

If the datasource list is empty, see [Grafana datasource list is empty](#grafana-datasource-list-is-empty) below.

**Manual connection** (standalone Grafana or wrong URL): **Connections → Add new connection → Prometheus**

| Grafana runs where | Prometheus URL |
|--------------------|----------------|
| This compose (`nso-grafana`) | `http://prometheus:9090` |
| Docker, Prometheus on Mac host | `http://host.docker.internal:9090` |
| Both on host (no Docker) | `http://127.0.0.1:9090` |

Then **Save & test**.

## Grafana: first query in Explore

Use this for a simple “is it working?” check.

1. **☰ menu → Explore**
2. Top left: datasource **Prometheus** (not “Mixed” or empty)
3. Query row **A** — click in the text field (or **Code** / `{ }` if you see a builder UI)
4. Type a PromQL query, e.g.:

   ```promql
   up
   ```

5. Click **Run query** (top right of the query panel), or press **Shift+Enter**

**You do not need** **Go queryless** or **Add query** for a first test — one query row **A** is enough.

| UI element | Use for first test? |
|------------|---------------------|
| Query box **A** | **Yes** — type PromQL here |
| **Run query** | **Yes** |
| **Go queryless** | No — metric browser only |
| **Add query** | No — only for a second metric (B, C…) |

**What you should see**

- Query `up` → table or graph with `job="pushgateway"`, `job="prometheus"`, value **1**
- After the [smoke test push](#smoke-test-push--scrape--query): `nso_test_metric` → value **1** (or **4** if you pushed a higher number)

Switch **Graph** / **Table** above the results. Set time range top-right (e.g. **Last 15 minutes**).

**Dashboards (optional):** **☰ → Dashboards → NSO → NSO Diagnostic** for scan results; **NSO Summary** shows the smoke-test metric.

## View what is in Pushgateway

Browser: http://localhost:9091 — metric groups by **job** / **instance**

Terminal:

```bash
curl -s http://localhost:9091/metrics | grep -E '^nso_|^another'
```

Pushgateway holds the **latest** pushed values; Prometheus scrapes them every **15s** into long-term storage; Grafana reads Prometheus.

## Smoke test (push → scrape → query)

```bash
# 1. Push a test metric (host → Pushgateway)
echo 'nso_test_metric 1' | curl --data-binary @- \
  'http://localhost:9091/metrics/job/nso-summary/instance/laptop'

# 2. Confirm Pushgateway has it
curl -s http://localhost:9091/metrics | grep nso_test_metric

# 3. Wait one scrape interval (15s), then query Prometheus
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=nso_test_metric' | python3 -m json.tool
```

Prometheus UI: http://localhost:9090/targets — job `pushgateway` should be **UP**.

Grafana: follow [Grafana: first query in Explore](#grafana-first-query-in-explore) with `nso_test_metric`, or open dashboard **NSO Summary**.

**Confirm Prometheus got the metric** (optional):

```bash
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=nso_test_metric' | python3 -m json.tool
```

Non-empty `"result"` → Prometheus has it; if Grafana is empty, check time range or query spelling (metric names are exact, e.g. `anothertest_metric` not `anotehrtest_metric`).

## File layout

```
deploy/monitoring/
  docker-compose.yml          # pushgateway + prometheus + grafana
  docker-compose.alerting.yml # opt-in: alert when scans stop arriving
  .env                        # optional, not in git: turns the scan alert on
  prometheus/
    prometheus.yml            # scrape config (edit scrape jobs here)
  grafana/
    provisioning/
      datasources/prometheus.yml
      dashboards/dashboards.yml
    alerting/
      nso-scan-alerts.yaml    # the alert rule and its Slack contact point (opt-in)
    dashboards/
      nso-diagnostic.json     # scan results (NSO Diagnostic)
      nso-summary.json        # Pushgateway smoke test (NSO Summary)
  README.md                   # this file
```

Edit **`prometheus/prometheus.yml`** for scrape intervals and extra jobs. Edit **`grafana/dashboards/`** for dashboards (reprovision on Grafana restart, or use UI save).

## Reload Prometheus after config change

`--web.enable-lifecycle` is enabled:

```bash
curl -X POST http://localhost:9090/-/reload
```

Or:

```bash
docker compose restart prometheus
```

## Using an existing Prometheus

If you keep your current `prometheus` container:

1. Merge the `pushgateway` job from `prometheus/prometheus.yml` into your live config.
2. Ensure Prometheus can reach Pushgateway:
   - Same Docker network: target `pushgateway:9091` or `nso-pushgateway:9091`
   - From container to host: `host.docker.internal:9091`
3. Start only Pushgateway and Grafana from this compose, or run Pushgateway standalone:

```bash
docker run -d --name nso-pushgateway -p 9091:9091 prom/pushgateway:v1.11.0
```

4. Point Grafana at your Prometheus URL in `grafana/provisioning/datasources/prometheus.yml` if not using compose networking.

## NSO agent integration

Set in the agent project `.env` (see `.env.example`), or answer yes to the Pushgateway
question in `nso-diagnostic-run --configure`:

```bash
PROMETHEUS_PUSHGATEWAY_URL=http://127.0.0.1:9091
PROMETHEUS_JOB=nso-summary        # the default job name
PROMETHEUS_INSTANCE=laptop        # default: default
```

After each scan that is not a dry run, the agent PUTs gauges to:

`http://localhost:9091/metrics/job/nso-summary/instance/<PROMETHEUS_INSTANCE>`

Each `PROMETHEUS_INSTANCE` is its own group, and Pushgateway keeps a group until it is
deleted. The NSO Diagnostic dashboard shows one agent at a time: pick it in the **Agent**
menu at the top. A setup you no longer scan from (for example after moving from a checkout
to Docker) keeps its last numbers there; remove its group with:

```bash
curl -X DELETE http://localhost:9091/metrics/job/nso-summary/instance/<PROMETHEUS_INSTANCE>
```

Every scan pushes these counts: `nso_summary_run_success`, `nso_summary_last_run_timestamp_seconds`, `nso_summary_run_duration_seconds`, `nso_fleet_devices_total`, `nso_fleet_devices_in_sync`, `nso_fleet_devices_out_of_sync` (configuration drift; a locked device is not counted), `nso_fleet_devices_locked`, `nso_fleet_devices_sync_error`, `nso_services_up` / `nso_services_down` (`service_type` label), `nso_isis_adjacencies_up` / `_down`, `nso_bgp_sessions_up` / `_down`, `nso_physical_links_up` / `_down`, `nso_infra_cpu_alerts` / `nso_infra_memory_alerts`, `nso_hardware_*_alerts`, `nso_device_cpu_five_min_percent` and `nso_device_memory_used_percent` (one series per device, `device` label), `nso_inventory_review_devices`, `nso_topology_issues` (`layer` label). Every series carries `pipeline="diagnostic"`. Changes since the previous scan are in the report and the Slack digest, not in metrics.

Unset `PROMETHEUS_PUSHGATEWAY_URL` to skip, or add `--skip-metrics` for one scan. If Pushgateway is down, the agent logs a warning and still finishes the report.

It also pushes:

| Metric | Meaning |
|---|---|
| `nso_services{service_type,status}` | The report's final status per service type: `up`, `down`, `degraded`, `unknown` |
| `nso_service_faults{device,service_type,status}` | Services that are not OpUp, by the device their failed check points to |
| `nso_fleet_devices_not_covered` | Devices dropped or not collected in the scan |
| `nso_link_crc_per_minute{device,port}` | CRC errors per minute on a link that is up, averaged over the gap since the previous scan; only links that gained CRC errors |
| `nso_link_state_changes{device,port}` | Times such a link went down or up since the previous scan; only links that changed |
| `nso_optics_alarm_ports` / `nso_optics_ports_read` | Ports on links that are up whose own device reports an optics alarm, and the ports the sweep read (a lead, not a status); absent when the scan did not sweep |
| `nso_link_utilization_percent{device,port,direction}` | The ten busiest port directions of the scan: the device's 5-minute rate over the port's bandwidth, one reading per scan (not a peak or a daily average); idle ports, ports that are down and devices read without CLI are left out; absent when no port carried traffic |
| `nso_scan_info{run_id,report_url}` | The scan the values come from; the link needs `DIAGNOSTIC_REPORT_BASE_URL` |
| `nso_scan_last_attempt_state` | How the most recent attempt ended, in one value: 0 ok, 1 failed, 2 NSO unreachable |
| `nso_scan_last_attempt_success`, `nso_scan_last_attempt_nso_unreachable`, `nso_scan_last_attempt_timestamp_seconds` | The same as two 0/1 gauges, and when it ended |
| `nso_llm_state` | How the scan's LLM work went: 0 complete, 1 partial, 2 unavailable, 3 skipped, 4 halted |
| `nso_llm_investigations_done`, `nso_llm_investigations_total` | Investigations that reached a conclusion, and all the scan started |
| `nso_llm_requests`, `nso_llm_input_tokens`, `nso_llm_output_tokens` | The scan's LLM usage |
| `nso_llm_cost_usd`, `nso_llm_budget_left_usd`, `nso_llm_budget_max_usd` | The scan's spend and the provider's budget, only when the provider reports them |
| `nso_llm_info{model}` | The model that made the scan's findings; none when the LLM was skipped |

A scan that ends without a report (for example NSO became unreachable) posts only the last three, with success 0. The last good scan's values stay in the Pushgateway, so compare `nso_summary_last_run_timestamp_seconds` with the current time to see how old they are.

Dashboard **NSO Diagnostic** (folder **NSO**, file `grafana/dashboards/nso-diagnostic.json`) charts these: the latest scan attempt, how long ago the last good scan finished, final service status, services not OpUp by device, links gaining errors while up (with a mark at each scan) beside the ten busiest port directions, physical, IS-IS and BGP links down, device sync, hardware and infrastructure (CPU, memory, sensors, fans, power, and ports with an optics alarm of their own), and the LLM work and cost.

## Alert when scans stop

Opt-in. Grafana posts to Slack when no published diagnostic scan has arrived for **48 hours**:
the scheduled scan stopped running (cron entry gone, Docker stopped). It watches
`nso_summary_last_run_timestamp_seconds`, which only a published scan updates, and posts again
when scans resume. A scan that runs but fails sends its own notice to the report's Slack and
email, so this alert is for scans that never start.

Alerting on findings is the agent's job, not Grafana's. Every published scan sends its report, with
what is new since the previous scan, to Slack and email. The dashboard shows the same numbers and
sends nothing. There is deliberately no Grafana alert on Down services, BGP or IS-IS sessions down
or links gaining errors: some faults stay open for weeks (a dark port, a lost adjacency), so an alert
on the count would fire all the time, while the report says which faults are new. To alert on one
of them from Grafana anyway, copy the rule in `nso-scan-alerts.yaml` and alert on a change, not on
the level.

Turn it on by creating a `.env` next to `docker-compose.yml`, in the folder the stack runs from
(see [Which folder the stack reads from](#which-folder-the-stack-reads-from)), with two lines. This file
is only for the monitoring stack, not the agent's `.env` or a Docker scan folder's
`nso-diagnostic.env`, and git ignores it, so the webhook URL stays out of the repository:

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.alerting.yml
NSO_ALERT_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

then run `docker compose up -d` in that folder. `COMPOSE_FILE` is Docker Compose's own setting: it makes
a plain `docker compose` combine the two files. The webhook can be the one the scans post to. Without it, compose stops
with a message rather than starting a Grafana that would refuse the alert. Grafana reads the
alert file when it starts: to change the 48 hours, edit `params: [48]` (a number of hours) in
`grafana/alerting/nso-scan-alerts.yaml` and run `docker compose restart grafana`.

Test it once: set the threshold to `0.5` (half an hour) and restart Grafana. Within about five minutes Slack gets
"No published NSO diagnostic scan", if the last published scan is older than half an hour. Set it
back to `48` and restart Grafana again; the alert clears at its next check. Grafana's alert list,
http://localhost:3000/alerting/list (**Alerting → Alert rules**, folder **NSO diagnostic**), shows
the rule's state at any time.

The file also sets Grafana's notification policy to this Slack contact point, so the policy can
no longer be edited in Grafana's UI. Not covered: the scan host itself down, or Prometheus or
Grafana stopped, since they run on the same host as the scans.

## Stop / remove

```bash
docker compose down          # keep volumes
docker compose down -v     # delete prometheus, grafana and pushgateway data
```

## Troubleshooting

| Problem | Check |
|---------|--------|
| Port already allocated | `docker ps`; stop conflicting container on 9090/9091/3000 |
| Prometheus empty query | Pushgateway UI shows metric? Targets UP? Wait 15s |
| Grafana no data | Data source **Save & test**; URL must be `http://prometheus:9090` inside compose |
| `localhost:9090` refused | `docker compose ps`; `docker logs nso-prometheus` |
| **Grafana datasource list empty** | See below |

### Grafana datasource list is empty

Provisioning only runs when Grafana is started **from this compose file** with the config volume mounted.

**1. Confirm you are using `nso-grafana` from this repo**

```bash
docker ps --filter name=grafana --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
```

You want **`nso-grafana`** (not a one-off `grafana/grafana` container without volumes).

**2. Start (or recreate) the stack from this directory**

```bash
cd /path/to/checkout/deploy/monitoring
docker compose up -d
docker compose restart grafana
```

**3. Verify provisioning files inside the container**

```bash
docker exec nso-grafana ls -la /etc/grafana/provisioning/datasources/
docker exec nso-grafana cat /etc/grafana/provisioning/datasources/prometheus.yml
docker logs nso-grafana 2>&1 | grep -i -E 'provisioning|datasource|error' | tail -20
```

**4. If still empty — add Prometheus manually (works immediately)**

In Grafana UI:

1. **☰ → Connections → Add new connection**
2. Choose **Prometheus**
3. **URL:**
   - Grafana from **this compose**: `http://prometheus:9090`
   - Standalone Grafana on Mac, Prometheus on host: `http://host.docker.internal:9090`
4. **Save & test** → must be green

**5. Wrong Grafana on port 3000**

If an old Grafana container owns `:3000`, stop it and use compose:

```bash
docker ps --format '{{.Names}} {{.Ports}}' | grep 3000
docker stop <other-grafana-container>
cd deploy/monitoring && docker compose up -d grafana
```
