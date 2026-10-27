# Building the agent image

For whoever prepares the image that [docs/DOCKER.md](../../docs/DOCKER.md) runs.

| File | What it is |
| ---- | ---------- |
| `build_image.sh` | Builds the image and, with `--save`, the file to hand over. |
| `docker-compose.yml` | Goes into the image; users copy it out in step 2 of docs/DOCKER.md. |
| `constraints.txt` | The package versions the image installs. |

## Build

You need a checkout of this repository, git, and Docker with buildx (part of current Docker
Engine, Docker Desktop and OrbStack). From the checkout:

```bash
deploy/agent/build_image.sh --save
```

It writes `dist/nso-diagnostic-agent-<version>-amd64.tar.gz` (about 80 MB). Git and Docker
builds ignore `dist/`. The image holds a copy of the code: rebuild it after every change to the
source, or scans keep running the old code.

- The script clones the [FABRIC NSO MCP server](https://github.com/fabric-testbed/fabric-nso-mcp-server)
  into a temporary folder, at the commit `install.py` pins (`MCP_REF`). That repository is
  private, so git needs the GitHub access you used for this checkout. It clones over SSH when
  this checkout was cloned over SSH, otherwise over HTTPS.
- To use a clone you already have, give its path first:
  `deploy/agent/build_image.sh /path/to/fabric-nso-mcp-server --save`.
- The image is for `linux/amd64`, the usual server, even when built on an Apple-silicon Mac.
  To run it on such a Mac, build it for ARM (add `--save` for a file for an ARM server):

  ```bash
  deploy/agent/build_image.sh --platform linux/arm64
  ```
- The version is today's date (UTC); set `VERSION` to choose it, as in
  `VERSION=1.0 deploy/agent/build_image.sh --save`. The image is also tagged `latest`.

No settings or saved scans go into the image: from this folder it takes only
`docker-compose.yml` and `constraints.txt`, and `.dockerignore` leaves out `.env`, `state/`
and any `nso-diagnostic.env`.

## Hand over

Give the file to whoever runs the agent (for example with `scp`); they follow docs/DOCKER.md.
If `docker-compose.yml` changed since their last image, tell them to compare it with theirs:

```bash
docker run --rm --entrypoint cat nso-diagnostic-agent /app/deploy/agent/docker-compose.yml | diff - docker-compose.yml
```

Or push the image to a registry; on the server, `docker pull` it and set `image:` in their
`docker-compose.yml` to its name:

```bash
docker tag nso-diagnostic-agent:2026.10.24 registry.example.com/nso-diagnostic-agent:2026.10.24
docker push registry.example.com/nso-diagnostic-agent:2026.10.24
```

## Package versions

The image uses Python 3.14 and the versions in `constraints.txt`, the ones live scans ran
with, so a later build installs the same. To move to newer versions, upgrade the `.venv` you
scan from, run a scan, then write the file again. A package not in the file is resolved when
the image is built.

```bash
.venv/bin/python -m pip freeze --exclude-editable | grep -v ' @ ' > deploy/agent/constraints.txt
```
