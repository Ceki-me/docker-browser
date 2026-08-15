#!/bin/bash
# Build the ceki headless-browser provider image.
#
# Run from the repo root:
#   ./build.sh
#
# What it does:
#   1. Stages the browser-extension dist into extension/ (git-ignored).
#   2. Runs `docker build` with the repo root as context.
#
# The extension dist is taken from:
#   $CEKI_EXT_DIST   (default: ../../browser-extension/dist)
#
# Optionally:  ./build.sh /path/to/browser-extension/dist
#
# One image: the bundled dist should be a PROD build — PROD URLs are the
# default at runtime.  Other environments (dev stand) are selected at runtime
# via CEKI_WS_URL / CEKI_API_URL (see entrypoint.sh).  The separate
# ceki/provider:dev image is deprecated.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${CEKI_IMAGE:-ceki/provider:latest}"
EXT_SRC="${1:-${CEKI_EXT_DIST:-}}"

if [ -z "${EXT_SRC:-}" ]; then
  # Try a couple of conventional locations for the extension clone.
  for cand in "$ROOT/../browser-extension/dist" "$HOME/browser-extension/dist"; do
    if [ -f "$cand/manifest.json" ]; then
      EXT_SRC="$cand"
      break
    fi
  done
fi

if [ -z "${EXT_SRC:-}" ] || [ ! -f "$EXT_SRC/manifest.json" ]; then
  echo "error: extension dist not found." >&2
  echo "  pass the dist dir explicitly:  $0 /path/to/browser-extension/dist" >&2
  echo "  or set CEKI_EXT_DIST." >&2
  exit 1
fi

echo "[ceki-provider] staging extension dist: $EXT_SRC -> extension/"
rm -rf "$ROOT/extension"
mkdir -p "$ROOT/extension"
cp -a "$EXT_SRC"/. "$ROOT/extension/"

echo "[ceki-provider] building image: $IMAGE"
docker build -t "$IMAGE" -f "$ROOT/Dockerfile" "$ROOT"

echo "[ceki-provider] done: $IMAGE"
echo "  run:  docker run --rm -e CEKI_PROVIDER_TOKEN=<token> $IMAGE"
