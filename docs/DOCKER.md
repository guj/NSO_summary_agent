# Docker

Run the NSO summary agent from a container — no local Python venv for the agent.

The image includes:

- `nso-summary-run` (this repo)
- `cisco-nso-mcp-server` from your local [fabric-nso-mcp-server](https://github.com/fabric-testbed/fabric-nso-mcp-server) checkout (passed in at build time)

You still need: Docker, reachability to **NSO** and **FABRIC AI**, and an env file with secrets.

For a non-Docker install, see [TRY_ME_OUT.md](TRY_ME_OUT.md).

## Build

You need **both** this repo and a checkout of the FABRIC MCP server (the full tool set). The Dockerfile does **not** `pip install` MCP from GitHub during the build — that often fails (auth/network) and can pull an incomplete upstream tool list.

```bash
# Paths are examples — use your real locations
export MCP_SRC=/path/to/fabric-nso-mcp-server
export AGENT_SRC=/path/to/NSO_summary_agent

cd "$AGENT_SRC"
docker build -t nso-summary-agent \
  --build-context mcp="$MCP_SRC" \
  .
```

On this machine that is typically:

```bash
cd /Users/dec2023/Work/ESnet/NSO_summary_agent
docker build -t nso-summary-agent \
  --build-context mcp=/Users/dec2023/Work/ESnet/MCP_server/fabric-nso-mcp-server \
  .
```

If you do not have the MCP repo yet:

```bash
git clone https://github.com/fabric-testbed/fabric-nso-mcp-server /path/to/fabric-nso-mcp-server
```

(Private clone may need your GitHub credentials on the **host**; the image build only copies files you already have.)

Requires Docker BuildKit (default on current Docker Desktop / OrbStack).

## Env file

Copy `.env.example` to a file **outside** the image (do not bake secrets into the image):

```bash
cp .env.example /path/to/nso-summary.env
# Edit: NSO_*, FABRIC_AI_API_KEY, optional Slack/SMTP
```

Notes:

| Variable | Docker tip |
| -------- | ---------- |
| `MCP_SERVER_CMD` | Optional — image defaults to `cisco-nso-mcp-server` on PATH. Do **not** point at a host `/Users/...` path |
| `STATE_DIR` | Optional — image defaults to `/app/state` (mount a volume there) |
| `NSO_ADDRESS` / `NSO_PASSWORD` / `FABRIC_AI_API_KEY` | Required (same as bare metal) |

## Run

```bash
# MCP connectivity
docker run --rm \
  --env-file /path/to/nso-summary.env \
  nso-summary-agent --list-tools

# Collect only (no LLM, no state write)
docker run --rm \
  --env-file /path/to/nso-summary.env \
  nso-summary-agent --skip-llm --dry-run

# Full report to stdout (no state / Slack / email)
docker run --rm \
  --env-file /path/to/nso-summary.env \
  nso-summary-agent --dry-run

# Persist state on the host + notify if Slack/SMTP configured
mkdir -p ./state
docker run --rm \
  --env-file /path/to/nso-summary.env \
  -v "$(pwd)/state:/app/state" \
  nso-summary-agent
```

Any CLI flags accepted by `nso-summary-run` work after the image name.

If the container cannot reach NSO on a private IP, use host networking where appropriate (Linux):

```bash
docker run --rm --network host \
  --env-file /path/to/nso-summary.env \
  nso-summary-agent --list-tools
```

On macOS/Windows Docker Desktop, host networking differs — prefer an NSO address the container can route to, or publish via VPN/DNS your Docker setup already uses.

## Schedule (3× daily)

Docker does not schedule itself. Use host cron (or launchd) to run the container:

```cron
0 8,14,20 * * * docker run --rm --env-file /etc/nso-summary.env -v /var/lib/nso-summary/state:/app/state nso-summary-agent >> /var/log/nso-summary.log 2>&1
```

Ensure the cron user can run Docker and read the env file.

## Optional: your own registry

```bash
docker tag nso-summary-agent registry.example.com/nso-summary-agent:0.1.0
docker push registry.example.com/nso-summary-agent:0.1.0
```

Others then `docker pull` and run with their own `--env-file`. They do **not** need the MCP source tree at run time (only at **build** time, unless you publish a pre-built image).

## Troubleshooting

| Symptom | Check |
| ------- | ----- |
| `build-context` / `mcp` errors | Pass `--build-context mcp=/path/to/fabric-nso-mcp-server` pointing at a real checkout |
| `--list-tools` shows only a few tools | Image was built from upstream-only MCP; rebuild with the FABRIC MCP tree |
| `--list-tools` fails with host `/Users/...` path | Remove `MCP_SERVER_CMD` from the env file (or set `cisco-nso-mcp-server`) |
| FABRIC errors | `FABRIC_AI_API_KEY` / URL; key not expired |
| Empty or missing `state/` | Pass `-v …:/app/state` and omit `DRY_RUN=1` for persist runs |
