# Docker

Run `nso-diagnostic-run` from a container, with no local Python environment for the agent.

The image contains:

- `nso-diagnostic-run` (this repo), as the entry point
- `cisco-nso-mcp-server`, built from your local [fabric-nso-mcp-server](https://github.com/fabric-testbed/fabric-nso-mcp-server) checkout

You still need Docker, network reachability from the container to NSO (and to your LLM endpoint unless you use `--skip-llm`), and an env file with your settings.

For a non-Docker install, see [DIAGNOSTIC_RUNNER.md](DIAGNOSTIC_RUNNER.md).

## Build

The build needs this repo and a checkout of the FABRIC MCP server. The Dockerfile copies the MCP server from that checkout instead of installing it from GitHub during the build.

```bash
git clone https://github.com/fabric-testbed/fabric-nso-mcp-server /path/to/fabric-nso-mcp-server

cd /path/to/checkout
docker build -t nso-diagnostic-agent \
  --build-context mcp=/path/to/fabric-nso-mcp-server \
  .
```

Requires Docker BuildKit (the default in current Docker Desktop and OrbStack).

## Env file

Copy `.env.example` to a file outside the image. Do not bake secrets into the image.

```bash
cp .env.example /path/to/nso-diagnostic.env
# Edit: NSO_ADDRESS, NSO_USERNAME, NSO_PASSWORD; FABRIC_AI_* for LLM runs; optional Slack/SMTP
```

| Variable | In the container |
| -------- | ---------------- |
| `MCP_SERVER_CMD` | Leave as `cisco-nso-mcp-server` (on PATH in the image). Do not use a host path. |
| `STATE_DIR` | Leave as `./state` or unset; both resolve to `/app/state`. Mount a volume there to keep reports. |
| `NSO_ADDRESS` / `NSO_PASSWORD` | Required. |
| `FABRIC_AI_API_KEY` | Required unless you pass `--skip-llm`. |

`docker run --env-file` passes values literally: do not quote them and do not use `export`.

## Run

Arguments after the image name go to `nso-diagnostic-run`.

```bash
# Show options
docker run --rm nso-diagnostic-agent --help

# Check the MCP server, NSO login, LLM key and delivery settings before a scan
docker run --rm --env-file /path/to/nso-diagnostic.env \
  nso-diagnostic-agent --check-connection

# Live collection and operational checks, no LLM, nothing saved or sent
docker run --rm --env-file /path/to/nso-diagnostic.env \
  nso-diagnostic-agent --skip-llm --dry-run

# LLM-assisted scan, report to stdout only
docker run --rm --env-file /path/to/nso-diagnostic.env \
  nso-diagnostic-agent --dry-run

# Save reports on the host and deliver to configured Slack/email
mkdir -p ./state
docker run --rm --env-file /path/to/nso-diagnostic.env \
  -v "$(pwd)/state:/app/state" \
  nso-diagnostic-agent --publish
```

Saved runs appear under `./state/diagnostic_mas/runs/<run-id>/` as `case.json`, `report.md` and `report.html`. Without `--publish` (or `DRY_RUN=0`) nothing is saved, even with the volume mounted. See [DIAGNOSTIC_RUNNER.md](DIAGNOSTIC_RUNNER.md) for all options.

If the container cannot reach NSO on a private address, Linux hosts can use host networking:

```bash
docker run --rm --network host --env-file /path/to/nso-diagnostic.env \
  nso-diagnostic-agent --skip-llm --dry-run
```

On macOS and Windows, Docker Desktop's host networking differs; use an NSO address the container can route to.

## Schedule

Docker does not schedule runs. Use host cron (or launchd) to start the container. The times below are an example:

```cron
0 8,14,20 * * * docker run --rm --env-file /etc/nso-diagnostic.env -v /var/lib/nso-diagnostic/state:/app/state nso-diagnostic-agent --publish >> /var/log/nso-diagnostic.log 2>&1
```

The cron user must be able to run Docker and read the env file.

## Optional: your own registry

```bash
docker tag nso-diagnostic-agent registry.example.com/nso-diagnostic-agent:0.1.0
docker push registry.example.com/nso-diagnostic-agent:0.1.0
```

Others can then pull the image and run it with their own `--env-file`. The MCP source tree is needed only at build time.

## Troubleshooting

| Symptom | Check |
| ------- | ----- |
| `build-context` or `mcp` errors during build | Pass `--build-context mcp=/path/to/fabric-nso-mcp-server` pointing at a real checkout |
| MCP server not found, or a host `/Users/...` path in the error | Remove `MCP_SERVER_CMD` from the env file, or set it to `cisco-nso-mcp-server` |
| Report shows zero devices and services | Collection failed: run `--check-connection`, then check `NSO_ADDRESS`, credentials and container-to-NSO reachability before trusting the result |
| LLM errors | Check `FABRIC_AI_API_KEY`, `FABRIC_AI_API_URL` and key expiry, or run with `--skip-llm` |
| No files under `state/` | Mount `-v …:/app/state` and pass `--publish` |
