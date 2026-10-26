#!/bin/sh
# Build the agent image, with the MCP server at the version install.py pins, for a server.
#
#   deploy/agent/build_image.sh [/path/to/fabric-nso-mcp-server] [--platform linux/arm64] [--save]
#
# Without a path, the MCP server is cloned into a temporary folder the way install.py clones
# it: over SSH when this checkout came over SSH, otherwise over HTTPS. The repository is
# private, so git needs the GitHub access you used for this checkout.
#
# The platform defaults to linux/amd64 (most servers), whatever this machine is. --save also
# writes dist/nso-diagnostic-agent-<version>-<arch>.tar.gz to hand over: load it with
# `docker load < file`. Set VERSION to tag a release; the default is today's date (UTC).
set -eu
cd "$(dirname "$0")/../.."

mcp=
case ${1:-} in
    ""|--*) ;;
    *) mcp=$1; shift ;;
esac
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

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
if [ -z "$mcp" ]; then
    case $(git remote get-url origin 2>/dev/null) in
        git@github.com:*|ssh://git@github.com/*) url=git@github.com:fabric-testbed/fabric-nso-mcp-server.git ;;
        *) url=https://github.com/fabric-testbed/fabric-nso-mcp-server.git ;;
    esac
    echo "cloning $url"
    mcp=$work/clone
    git clone --quiet "$url" "$mcp" || {
        echo "Could not clone the MCP server. The repository is private: git needs GitHub access to" >&2
        echo "fabric-testbed/fabric-nso-mcp-server, as for this checkout. Or give the path of a clone you have." >&2
        exit 1
    }
fi

# Only the pinned version goes into the image, nothing else from that clone.
src=$work/src
mkdir "$src"
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
