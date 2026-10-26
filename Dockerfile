# syntax=docker/dockerfile:1

# The Python the agent's live scans run on.
FROM python:3.14-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# FABRIC MCP sources via BuildKit additional context (not pip+git), at the version this
# agent was tested with: deploy/agent/build_image.sh passes that version. See docs/DOCKER.md.
COPY --from=mcp pyproject.toml README.md LICENSE* /tmp/fabric-nso-mcp-server/
COPY --from=mcp cisco_nso_mcp_server /tmp/fabric-nso-mcp-server/cisco_nso_mcp_server

# Editable install keeps Path(__file__) under /app so config/ resolves.
# .env.example and the compose file ship in the image, so the image alone is enough to set up.
# Only named files from deploy/agent: settings or saved runs left there never reach the image.
COPY pyproject.toml README.md .env.example ./
COPY agent ./agent
COPY nso_facts ./nso_facts
COPY nso_report ./nso_report
COPY multi_agent ./multi_agent
COPY diagnostic_mas ./diagnostic_mas
COPY config ./config
COPY deploy/agent/docker-compose.yml deploy/agent/constraints.txt ./deploy/agent/

# Pinned to the tested versions (deploy/agent/constraints.txt), so a later build installs the same.
RUN pip install --no-cache-dir -c deploy/agent/constraints.txt /tmp/fabric-nso-mcp-server \
    && pip install --no-cache-dir -c deploy/agent/constraints.txt -e . \
    && which cisco-nso-mcp-server \
    && which nso-diagnostic-run \
    && rm -rf /tmp/fabric-nso-mcp-server

# In-image MCP binary and state folder; settings come from a .env mounted at /app/.env.
# HOME is set so the container also runs under a user id the image does not know.
# The versions are pinned, so the MCP server's start-up check for a newer fastmcp is off.
ENV MCP_SERVER_CMD=cisco-nso-mcp-server \
    STATE_DIR=/app/state \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    FASTMCP_CHECK_FOR_UPDATES=off

# Not root: saved runs on a mounted state folder belong to the user the container runs as
# (deploy/agent/docker-compose.yml runs it as the host user).
RUN useradd --uid 1000 --no-create-home --home-dir /tmp agent \
    && mkdir -p /app/state \
    && chown agent /app/state
USER agent

ARG VERSION=dev
LABEL org.opencontainers.image.title="nso-diagnostic-agent" \
      org.opencontainers.image.version="$VERSION"

ENTRYPOINT ["nso-diagnostic-run"]
CMD []
