# Run the agent in Docker

The image holds the agent and the NSO MCP server, installed and ready to run. Run it on a
server next to NSO, so no VPN is needed.

You need:

- Linux with Docker and its compose plugin (`docker compose version` prints a version).
- Access from the server to NSO's HTTPS port, the LLM endpoint, and Slack or your mail server
  if reports go there.
- The image file, such as `nso-diagnostic-agent-2026.10.24-amd64.tar.gz`, from the
  maintainers. They make it from this repository as described in
  [deploy/agent/README.md](../deploy/agent/README.md); with a checkout you can build it yourself.

## 1. Load the image

```bash
docker load < nso-diagnostic-agent-2026.10.24-amd64.tar.gz
```

You should see `Loaded image: nso-diagnostic-agent:latest`.

No image file? Build it from a checkout of this repository instead, for the machine you run
it on. On a Linux server:

```bash
deploy/agent/build_image.sh
```

On an Apple-silicon Mac:

```bash
deploy/agent/build_image.sh --platform linux/arm64
```

The image is then in Docker; go on to step 2.

## 2. Make the agent's folder

Run these as the user who will run the scans. Use any folder outside a checkout: replace
`/your/nso-diagnostic-dir` here and below with yours, such as `~/nso-diagnostic`.

```bash
mkdir -p /your/nso-diagnostic-dir/state && cd /your/nso-diagnostic-dir
docker run --rm --entrypoint cat nso-diagnostic-agent /app/deploy/agent/docker-compose.yml > docker-compose.yml
docker run --rm --entrypoint cat nso-diagnostic-agent /app/.env.example > nso-diagnostic.env
chmod 600 nso-diagnostic.env
printf 'AGENT_UID=%s\nAGENT_GID=%s\n' "$(id -u)" "$(id -g)" > .env
```

Run every `docker compose` command below from this folder. Keep its `state/`: each scan
compares with the last one saved there.

## 3. Enter the settings

```bash
docker compose run --rm agent --configure
```

Answer the questions: NSO first, then the optional LLM, Slack, email and Pushgateway. They are
saved in `nso-diagnostic.env`. Answer no to the connection check at the end; step 4 does it.

## 4. Check the connection

```bash
docker compose run --rm agent --check-connection
```

Every line should say OK, such as `MCP server: OK (33 tools)` and `NSO: OK (<n> devices)`.

## 5. Scan

```bash
docker compose run --rm agent --publish
```

The report is saved in `state/diagnostic_mas/runs/<run-id>/` and sent to Slack or email. To
try a scan that saves and sends nothing, use `--skip-llm --dry-run` instead of `--publish`.

Other [options](DIAGNOSTIC_RUNNER.md#running) also go after `agent`; the example below has
our usual larger scan.

## Example: start to finish on an Apple-silicon Mac

How we tried it on a Mac with OrbStack, from a checkout of this repository. First replace
`/path/to/checkout` and `/your/nso-diagnostic-dir` with your own folders. Paste one block at a
time: the third one asks questions.

```bash
cd /path/to/checkout
deploy/agent/build_image.sh --platform linux/arm64
```

```bash
mkdir -p /your/nso-diagnostic-dir/state && cd /your/nso-diagnostic-dir
docker run --rm --entrypoint cat nso-diagnostic-agent /app/deploy/agent/docker-compose.yml > docker-compose.yml
docker run --rm --entrypoint cat nso-diagnostic-agent /app/.env.example > nso-diagnostic.env
chmod 600 nso-diagnostic.env
printf 'AGENT_UID=%s\nAGENT_GID=%s\n' "$(id -u)" "$(id -g)" > .env
```

```bash
cd /your/nso-diagnostic-dir && docker compose run --rm agent --configure
```

```bash
cd /your/nso-diagnostic-dir && docker compose run --rm agent --check-connection
```

```bash
cd /your/nso-diagnostic-dir && mkdir -p log
{ time docker compose run --rm -T agent \
    --spine-concurrent-devices 6 \
    --dataplane-concurrent_works 2 \
    --max-dataplane-per-category 30 \
    --max-dataplane-tools 40 \
    --max-drill-issues 2 \
    --publish; } > log/test.txt 2>&1
```

Our scan took about 50 minutes for 34 devices and about 2,050 services. The log is in
`log/test.txt` (`-T` keeps it plain text) and the report in `state/diagnostic_mas/runs/`.

## Scheduled scans

Use cron on the server; the times are an example:

```cron
0 8,14,20 * * * cd /your/nso-diagnostic-dir && docker compose run --rm -T agent --publish >> scans.log 2>&1
```

## Upgrade

Load the new image file, then run step 4 again. Settings and saved scans stay as they are.
If you build the image from a checkout, rebuild it after each update of the checkout: the
image holds its own copy of the code, so scans run the old code until you do.

```bash
docker load < nso-diagnostic-agent-<new-version>-amd64.tar.gz
```

To go back, `docker tag nso-diagnostic-agent:<old-version> nso-diagnostic-agent:latest`
(`docker images nso-diagnostic-agent` lists the versions).

## Special cases

- **Coming from an install.py setup:** instead of step 3, copy its settings, and its saved
  scans to keep the history:

  ```bash
  cp /path/to/checkout/.env /your/nso-diagnostic-dir/nso-diagnostic.env && chmod 600 /your/nso-diagnostic-dir/nso-diagnostic.env
  cp -a /path/to/checkout/state/. /your/nso-diagnostic-dir/state/
  ```

- **NSO certificate from a private CA:** copy the bundle into the folder, add
  `- ./nso-ca.pem:/app/nso-ca.pem:ro` under `volumes:` in `docker-compose.yml`, and set
  `NSO_CA_BUNDLE=/app/nso-ca.pem`.
- **Pushgateway:** `PROMETHEUS_PUSHGATEWAY_URL=http://127.0.0.1:9091` reaches one on the same
  server. Set `PROMETHEUS_INSTANCE` to name this agent in the Grafana dashboard. The
  monitoring stack itself is in `deploy/monitoring/README.md`.

## Troubleshooting

| Symptom | Check |
| ------- | ----- |
| `nso-diagnostic.env` is a folder | It did not exist at the first run, so Docker made a folder. Remove it and redo step 2. |
| `Permission denied` under `state/` | `state/` must belong to you (`ls -ld state`). Docker creates it as root when it is missing. |
| `--configure` only lists missing settings | It had no terminal. Run it from an interactive shell, without `-T`. |
| `NSO: FAILED` with a name or timeout error | Check that the server itself reaches NSO; the container uses the server's network. |
| NSO certificate errors | See the private CA case above. |
| A password with quotes does not work | Do not use `docker run --env-file`; it keeps the quotes. Use the compose file, which mounts the settings. |
| LLM errors | Check `FABRIC_AI_API_KEY` and `FABRIC_AI_API_URL`, or scan with `--skip-llm`. |
| `exec format error` | The image was built for another CPU; ask for one that matches `uname -m`. |
| Nothing under `state/` | Only `--publish` saves scans. |

On a Mac, a container may not reach NSO through the Mac's VPN; if so, use `install.py` there.
