#!/bin/sh
# Build the agent image, with the MCP server at the version install.py pins, for a server.
#
#   deploy/agent/build_image.sh /path/to/fabric-nso-mcp-server [--platform linux/arm64] [--save]
#
# The platform defaults to linux/amd64 (most servers), whatever this machine is. --save also
# writes dist/nso-diagnostic-agent-<version>-<arch>.tar.gz to hand over: load it with
# `docker load < file`. Set VERSION to tag a release; the default is today's date (UTC).
set -eu
cd "$(dirname "$0")/../.."

mcp=${1:?usage: deploy/agent/build_image.sh /path/to/fabric-nso-mcp-server [--platform P] [--save]}
shift
platform=linux/amd64
save=
while [ $# -gt 0 ]; do
    case $1 in
        --platform) platform=$2; shift 2 ;;
        --save) save=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

ref=$(sed -n 's/^MCP_REF = "\([0-9a-f]*\)".*/\1/p' install.py)
[ -n "$ref" ] || { echo "no MCP_REF in install.py" >&2; exit 2; }
version=${VERSION:-$(date -u +%Y.%m.%d)}

# Only the pinned version goes into the image, nothing else from that clone.
src=$(mktemp -d)
trap 'rm -rf "$src"' EXIT
git -C "$mcp" cat-file -e "$ref^{commit}" 2>/dev/null || git -C "$mcp" fetch --quiet origin
git -C "$mcp" archive "$ref" | tar -x -C "$src"

docker buildx build --platform "$platform" --build-context mcp="$src" --build-arg VERSION="$version" \
    -t "nso-diagnostic-agent:$version" -t nso-diagnostic-agent:latest --load .
echo "built nso-diagnostic-agent:$version ($platform) with the MCP server at ${ref%"${ref#????????????}"}"

if [ -n "$save" ]; then
    mkdir -p dist
    out="dist/nso-diagnostic-agent-$version-${platform##*/}.tar.gz"
    docker save "nso-diagnostic-agent:$version" nso-diagnostic-agent:latest | gzip > "$out"
    echo "wrote $out"
fi
