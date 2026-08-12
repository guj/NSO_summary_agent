# syntax=docker/dockerfile:1

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# FABRIC MCP sources via BuildKit additional context (not pip+git).
# Build with:
#   docker build -t nso-summary-agent \
#     --build-context mcp=/path/to/fabric-nso-mcp-server \
#     .
# See docs/DOCKER.md
COPY --from=mcp pyproject.toml README.md LICENSE* /tmp/fabric-nso-mcp-server/
COPY --from=mcp cisco_nso_mcp_server /tmp/fabric-nso-mcp-server/cisco_nso_mcp_server

# Editable install keeps Path(__file__) under /app so prompts/ and config/ resolve.
COPY pyproject.toml README.md ./
COPY agent ./agent
COPY prompts ./prompts
COPY config ./config

RUN pip install --no-cache-dir /tmp/fabric-nso-mcp-server \
    && pip install --no-cache-dir -e . \
    && which cisco-nso-mcp-server \
    && which nso-summary-run \
    && rm -rf /tmp/fabric-nso-mcp-server

# In-image MCP binary (override with --env-file if needed)
ENV MCP_SERVER_CMD=cisco-nso-mcp-server \
    STATE_DIR=/app/state \
    PYTHONUNBUFFERED=1

RUN mkdir -p /app/state

ENTRYPOINT ["nso-summary-run"]
CMD []
