# Docker

Run the agent in a container on a server next to NSO, so no VPN is needed. The image holds
the agent and the NSO MCP server at the versions live scans ran with; the server needs only
Docker. The settings and the saved runs stay in one folder on the server.

For an installation without Docker, see [DIAGNOSTIC_RUNNER.md](DIAGNOSTIC_RUNNER.md).
To build the image, see [Build the image](#build-the-image).

## What the server needs

- Linux with Docker Engine and its compose plugin (`docker compose version` prints a version).
- Network access from the server to NSO's HTTPS port, to the LLM endpoint (unless scans run
  with `--skip-llm`), and to Slack or the SMTP server if reports are delivered there. The
  container uses the server's own network.
- The image file, `nso-diagnostic-agent-<version>-amd64.tar.gz`, or a registry it was
  pushed to.

## 1. Load the image

```bash
docker load < nso-diagnostic-agent-2026.10.24-amd64.tar.gz
```

```bash
docker run --rm nso-diagnostic-agent --help
```

`docker load` adds the tags `nso-diagnostic-agent:<version>` and `nso-diagnostic-agent:latest`.

## 2. Make the agent's folder

Run these as the user who will run the scans; that user needs access to Docker. The folder
can be anywhere; the examples use `~/nso-diagnostic`.

```bash
mkdir -p ~/nso-diagnostic/state && cd ~/nso-diagnostic
docker run --rm --entrypoint cat nso-diagnostic-agent /app/deploy/agent/docker-compose.yml > docker-compose.yml
docker run --rm --entrypoint cat nso-diagnostic-agent /app/.env.example > nso-diagnostic.env
chmod 600 nso-diagnostic.env
printf 'AGENT_UID=%s\nAGENT_GID=%s\n' "$(id -u)" "$(id -g)" > .env
```

| In the folder | What it is |
| ------------- | ---------- |
| `docker-compose.yml` | How the container runs: the server's network, your user ID, and the two mounts below. |
| `nso-diagnostic.env` | The agent's settings: NSO login, LLM key, Slack and email. The container reads it as `/app/.env`. |
| `.env` | Only `AGENT_UID` and `AGENT_GID`, for `docker compose`, so the saved files belong to you. |
| `state/` | Saved runs. Each scan compares with the last one, so keep this folder. |

Create `nso-diagnostic.env` and `state/` before the first run, as above. Docker makes a
missing mount source into an empty folder owned by root, and the agent then can neither read
its settings nor save runs.

## 3. Answer the setting questions

```bash
docker compose run --rm agent --configure
```

The questions are the ones `install.py` asks; see
[step 3 of the diagnostic guide](DIAGNOSTIC_RUNNER.md#3-answer-the-setting-questions). The
last line says `Saved … to /app/.env`: that is `nso-diagnostic.env` in this folder. You can
also edit that file directly; keep it private.

- Do not pass the settings with `docker run --env-file`. It reads values literally, so the
  quotes in the file would become part of a password. Mount the file, as the compose file does.
- `MCP_SERVER_CMD` and `STATE_DIR` are set by the image; values for them in the file are
  ignored.
- If NSO's certificate is from a private CA, copy the CA bundle into the folder, add the line
  `- ./nso-ca.pem:/app/nso-ca.pem:ro` under `volumes:` in `docker-compose.yml`, and give
  `/app/nso-ca.pem` as the CA bundle path (`NSO_CA_BUNDLE`). The path is the one inside the
  container.

## 4. Check, then scan

```bash
docker compose run --rm agent --check-connection
```

```bash
docker compose run --rm agent --skip-llm --dry-run
```

```bash
docker compose run --rm agent --publish
```

Options after `agent` go to `nso-diagnostic-run`, with the meanings in
[the diagnostic guide](DIAGNOSTIC_RUNNER.md#running): `--check-connection` checks NSO, the
LLM key and delivery without scanning; `--dry-run` scans and prints the report but saves and
sends nothing; `--publish` saves the run under `state/diagnostic_mas/runs/<run-id>/`
(`case.json`, `report.md`, `report.html`) and delivers it to Slack or email if configured.

## Schedule

Docker does not schedule runs; use cron on the server. The times below are an example:

```cron
0 8,14,20 * * * cd "$HOME/nso-diagnostic" && docker compose run --rm -T agent --publish >> scans.log 2>&1
```

`-T` runs without a terminal, as cron has none.

## Pushgateway, Prometheus and Grafana (optional)

The container uses the server's network, so `PROMETHEUS_PUSHGATEWAY_URL=http://127.0.0.1:9091`
reaches a Pushgateway on the same server. The agent only sends the counts; the monitoring
stack is not in the image. To run it, see `deploy/monitoring/README.md` in this repository.

## Upgrade

```bash
docker load < nso-diagnostic-agent-<new-version>-amd64.tar.gz
```

```bash
cd ~/nso-diagnostic && docker compose run --rm agent --check-connection
```

The settings and `state/` stay as they are. To see whether the new image's compose file
differs from yours (your own lines, such as a CA mount, show up too):

```bash
docker run --rm --entrypoint cat nso-diagnostic-agent /app/deploy/agent/docker-compose.yml | diff - docker-compose.yml
```

To go back to an earlier version (`docker images nso-diagnostic-agent` lists them):

```bash
docker tag nso-diagnostic-agent:<old-version> nso-diagnostic-agent:latest
```

## Without compose

The same run with `docker run`, from the agent's folder. Add `-it` for `--configure`.

```bash
docker run --rm --network host --user "$(id -u):$(id -g)" -v "$PWD/nso-diagnostic.env:/app/.env" -v "$PWD/state:/app/state" nso-diagnostic-agent --check-connection
```

## Build the image

This part is for whoever prepares the image. It needs a checkout of this repository, Docker
with buildx (part of current Docker Engine, Docker Desktop and OrbStack), and a clone of the
[FABRIC NSO MCP server](https://github.com/fabric-testbed/fabric-nso-mcp-server). That
repository is private, so the clone needs your GitHub sign-in, as in
[step 1 of the diagnostic guide](DIAGNOSTIC_RUNNER.md#1-get-this-repository).

```bash
deploy/agent/build_image.sh /path/to/fabric-nso-mcp-server --save
```

- The image is built for `linux/amd64`, the usual server, also on an Apple-silicon Mac. For
  an ARM server, add `--platform linux/arm64`.
- The MCP server goes in at the commit `install.py` pins (`MCP_REF`). The script fetches
  that commit into the clone if it is missing.
- The tags are `nso-diagnostic-agent:<today's date, UTC>` and `:latest`. Set `VERSION` to
  choose the version, as in `VERSION=1.0 deploy/agent/build_image.sh …`.
- `--save` writes `dist/nso-diagnostic-agent-<version>-<arch>.tar.gz` (about 80 MB) to hand
  over. Git and Docker builds ignore `dist/`.
- No settings or saved runs go into the image: `.dockerignore` leaves out `.env` and `state/`.

The image uses Python 3.14 and the package versions in `deploy/agent/constraints.txt`, the
ones live scans ran with, so a later build installs the same. To move to newer versions,
upgrade the `.venv` you scan from, run a scan, then write the file again. A package that is
not in the file is resolved when the image is built.

```bash
.venv/bin/python -m pip freeze --exclude-editable | grep -v ' @ ' > deploy/agent/constraints.txt
```

To hand over through a registry instead of a file, push the image there; on the server,
`docker pull` it and set `image:` in `docker-compose.yml` to its name.

```bash
docker tag nso-diagnostic-agent:2026.10.24 registry.example.com/nso-diagnostic-agent:2026.10.24
docker push registry.example.com/nso-diagnostic-agent:2026.10.24
```

## Troubleshooting

| Symptom | Check |
| ------- | ----- |
| `nso-diagnostic.env` is a folder, or the settings are not found | The file did not exist at the first run, so Docker made a folder. Remove that empty folder and create the file as in step 2. |
| `Permission denied` under `state/` | `ls -ld state`: it must belong to the user in `.env` (`AGENT_UID`, `AGENT_GID`). Docker creates it as root when it is missing. |
| `--configure` exits with a list of missing settings | It had no terminal to ask in. Run it from an interactive shell, without `-T`. |
| NSO check fails with a name or timeout error | The container uses the server's network: check that the server itself resolves `NSO_ADDRESS` and reaches its HTTPS port. |
| NSO certificate errors | `NSO_CA_BUNDLE` must be the path inside the container, with the file mounted (step 3). |
| `exec format error`, or a platform warning | The image is for another CPU. Build it with the `--platform` that matches `uname -m`: `x86_64` is `linux/amd64`, `aarch64` is `linux/arm64`. |
| The report shows zero devices and services | Collection failed. Run `--check-connection` before trusting the result. |
| LLM errors | Check `FABRIC_AI_API_KEY`, `FABRIC_AI_API_URL` and the key's expiry, or scan with `--skip-llm`. |
| Nothing under `state/` | Only `--publish` saves runs. |

The image is meant for Linux servers. On a Mac, Docker's host networking differs: a container
may not reach networks the Mac reaches through a VPN. Use `install.py` there instead.
