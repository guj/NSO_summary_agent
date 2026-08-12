# Prometheus + Grafana (local monitoring stack)

Optional **Pushgateway → Prometheus → Grafana** stack for NSO summary time-series metrics. The NSO agent runs on the **host** (cron/launchd); this compose file runs the metrics backends in Docker.

```
nso-summary-run (host)  --POST-->  Pushgateway :9091
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
cd /Users/dec2023/Work/ESnet/NSO_summary_agent/deploy/monitoring

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

Grafana provisions the **Prometheus** datasource and loads dashboard **NSO Summary** (folder **NSO**).

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

**Dashboard (optional):** **☰ → Dashboards → NSO → NSO Summary**

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
  prometheus/
    prometheus.yml            # scrape config (edit scrape jobs here)
  grafana/
    provisioning/
      datasources/prometheus.yml
      dashboards/dashboards.yml
    dashboards/
      nso-summary.json        # starter dashboard
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

If you keep your current `prometheus` container (e.g. `4efce14ee42c`):

1. Merge the `pushgateway` job from `prometheus/prometheus.yml` into your live config.
2. Ensure Prometheus can reach Pushgateway:
   - Same Docker network: target `pushgateway:9091` or `nso-pushgateway:9091`
   - From container to host: `host.docker.internal:9091`
3. Start only Pushgateway and Grafana from this compose, or run Pushgateway standalone:

```bash
docker run -d --name nso-pushgateway -p 9091:9091 prom/pushgateway:v1.11.0
```

4. Point Grafana at your Prometheus URL in `grafana/provisioning/datasources/prometheus.yml` if not using compose networking.

## NSO agent integration (Phase 1)

Set in the agent project `.env` (see `.env.example`):

```bash
PROMETHEUS_PUSHGATEWAY_URL=http://127.0.0.1:9091
PROMETHEUS_JOB=nso-summary
PROMETHEUS_INSTANCE=laptop
```

After each successful **non-dry-run**, the agent PUTs gauges to:

`http://localhost:9091/metrics/job/nso-summary/instance/<PROMETHEUS_INSTANCE>`

Phase 1+ names include: `nso_summary_run_success`, `nso_summary_last_run_timestamp_seconds`, `nso_summary_run_duration_seconds`, `nso_fleet_devices_total`, `nso_fleet_devices_in_sync`, `nso_fleet_devices_out_of_sync`, `nso_fleet_devices_sync_error`, `nso_services_up` / `nso_services_down` (`service_type` label), `nso_isis_adjacencies_up` / `_down`, `nso_bgp_sessions_up` / `_down`, `nso_physical_links_up` / `_down`, `nso_infra_cpu_alerts` / `nso_infra_memory_alerts`, `nso_hardware_*_alerts`, `nso_inventory_review_devices`, `nso_delta_*`, `nso_topology_issues` (`layer` label). Every series includes `pipeline="agent"` or `pipeline="multi-agent"`.

Unset `PROMETHEUS_PUSHGATEWAY_URL` to skip. If Pushgateway is down, the agent logs a warning and still finishes the report.

## Stop / remove

```bash
docker compose down          # keep volumes
docker compose down -v     # delete prometheus + grafana data
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
cd /Users/dec2023/Work/ESnet/NSO_summary_agent/deploy/monitoring
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
