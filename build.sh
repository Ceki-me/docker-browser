#!/bin/bash
# Build the ceki headless-browser provider image.
#
# Run from the repo root:
#   ./build.sh [SOURCE]
#
# SOURCE — where the browser-extension dist comes from (extension/ is staged
# from it, then `docker build`). Resolved by priority:
#
#   1. explicit source (first match wins):
#        --url  <URL>        download a release asset (zip or crx) by URL
#        --zip  <file.zip>   unpack a local zip
#        --crx  <file.crx>   unpack a local crx (Chrome extension archive)
#        --dir  <path>       copy an unpacked dist directory (e.g. a local build)
#   2. environment:
#        $CEKI_EXT_URL | $CEKI_EXT_ZIP | $CEKI_EXT_CRX | $CEKI_EXT_DIST
#   3. default: the latest published extension release from the prod host:
#        https://browser.ceki.me/ext/ceki-browser-extension-latest.zip
#      (the same release bundle the extension updater serves from /ext/)
#   4. fallback: a local clone of browser-extension
#        (../browser-extension/dist or ~/browser-extension/dist)
#
# Backwards compatible: `./build.sh /path/to/dist` (bare path = --dir).
#
# One image: the bundled dist should be a PROD build — PROD URLs are the
# default at runtime.  The separate ceki/provider:dev image is deprecated.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${CEKI_IMAGE:-ceki/provider:latest}"

# ---------------------------------------------------------------------------
# Resolve the extension source.
# ---------------------------------------------------------------------------
EXT_SRC=""
EXT_KIND=""

# 1. explicit source: --url/--zip/--crx/--dir (or a bare path = --dir).
while [ "$#" -gt 0 ]; do
  case "$1" in
    --url) EXT_SRC="$2"; EXT_KIND="auto"; shift 2 ;;
    --zip) EXT_SRC="$2"; EXT_KIND="zip";  shift 2 ;;
    --crx) EXT_SRC="$2"; EXT_KIND="crx";  shift 2 ;;
    --dir) EXT_SRC="$2"; EXT_KIND="dir";  shift 2 ;;
    --*)   echo "error: unknown flag: $1" >&2; exit 1 ;;
    *)     EXT_SRC="$1"; EXT_KIND="dir";  shift 1 ;;
  esac
done

# 2. environment.
if [ -z "$EXT_SRC" ]; then
  if [ -n "${CEKI_EXT_URL:-}" ]; then EXT_SRC="$CEKI_EXT_URL"; EXT_KIND="auto"
  elif [ -n "${CEKI_EXT_ZIP:-}" ]; then EXT_SRC="$CEKI_EXT_ZIP"; EXT_KIND="zip"
  elif [ -n "${CEKI_EXT_CRX:-}" ]; then EXT_SRC="$CEKI_EXT_CRX"; EXT_KIND="crx"
  elif [ -n "${CEKI_EXT_DIST:-}" ]; then EXT_SRC="$CEKI_EXT_DIST"; EXT_KIND="dir"
  fi
fi

# 3. default: latest published extension release from the prod host.
DEFAULT_URL="https://browser.ceki.me/ext/ceki-browser-extension-latest.zip"
if [ -z "$EXT_SRC" ]; then
  EXT_SRC="$DEFAULT_URL"
  EXT_KIND="auto"
fi

# Classify URL sources by the path extension (github release assets keep it).
if [ "$EXT_KIND" = "auto" ]; then
  case "$EXT_SRC" in
    http://*|https://*) case "$EXT_SRC" in
                          *.zip) EXT_KIND="url-zip" ;;
                          *.crx) EXT_KIND="url-crx" ;;
                          *)     EXT_KIND="url-zip" ;; # no ext → assume zip
                        esac ;;
    *.zip)              EXT_KIND="zip" ;;
    *.crx)              EXT_KIND="crx" ;;
    *)                  EXT_KIND="dir" ;;
  esac
fi

# ---------------------------------------------------------------------------
# Stage the extension dist into extension/.
# ---------------------------------------------------------------------------
stage_ext() { # $1 = src, $2 = kind
  local src="$1" kind="$2" tmp
  echo "[ceki-provider] extension source: $src ($kind)"
  rm -rf "$ROOT/extension"
  mkdir -p "$ROOT/extension"

  case "$kind" in
    dir)
      if [ ! -f "$src/manifest.json" ]; then
        echo "error: $src/manifest.json not found (not an unpacked extension dist)." >&2
        exit 1
      fi
      cp -a "$src"/. "$ROOT/extension/"
      ;;
    url-zip|url-crx)
      tmp="$(mktemp)"
      trap 'rm -f "$tmp"' EXIT
      echo "[ceki-provider] downloading: $src"
      if ! curl -fsSL "$src" -o "$tmp"; then
        echo "error: download failed: $src" >&2
        exit 1
      fi
      echo "[ceki-provider] downloaded $(du -h "$tmp" | cut -f1)"
      # Re-dispatch on the downloaded file type.
      if [ "$kind" = "url-crx" ]; then
        unpack_crx "$tmp"
      else
        unpack_zip "$tmp"
      fi
      ;;
    zip)
      [ -f "$src" ] || { echo "error: zip not found: $src" >&2; exit 1; }
      unpack_zip "$src"
      ;;
    crx)
      [ -f "$src" ] || { echo "error: crx not found: $src" >&2; exit 1; }
      unpack_crx "$src"
      ;;
  esac

  if [ ! -f "$ROOT/extension/manifest.json" ]; then
    echo "error: staged dist has no manifest.json — not a valid extension bundle." >&2
    exit 1
  fi
  echo "[ceki-provider] staged extension -> extension/ (manifest.json OK)"
}

unpack_zip() {
  local z="$1"
  # Unzip preserving the bundle root; strips a single top-level dir if present.
  local tmpd; tmpd="$(mktemp -d)"
  trap 'rm -rf "$tmpd"' EXIT
  unzip -q "$z" -d "$tmpd"
  if [ -f "$tmpd/manifest.json" ]; then
    cp -a "$tmpd"/. "$ROOT/extension/"
  else
    local sub; sub="$(find "$tmpd" -mindepth 1 -maxdepth 1 -type d | head -1)"
    if [ -n "$sub" ] && [ -f "$sub/manifest.json" ]; then
      cp -a "$sub"/. "$ROOT/extension/"
    else
      echo "error: no manifest.json inside zip." >&2
      exit 1
    fi
  fi
}

unpack_crx() {
  local c="$1"
  # A CRX is a zip with a signed header prefix. Python's zipfile locates the
  # central directory from the end, so it unpacks CRX3 archives as-is.
  python3 - "$c" "$ROOT/extension" <<'PY'
import sys, zipfile, os
src, dst = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(src) as z:
    names = z.namelist()
    if "manifest.json" in names:
        z.extractall(dst)
    else:
        top = {n.split("/")[0] for n in names if "/" in n}
        if len(top) == 1:
            root = next(iter(top))
            for n in names:
                if n == root or n.startswith(root + "/"):
                    z.extract(n, dst)
        else:
            z.extractall(dst)
PY
  # If the archive had a single top dir, move its contents up.
  if [ -f "$ROOT/extension/manifest.json" ]; then :; else
    local sub; sub="$(find "$ROOT/extension" -mindepth 1 -maxdepth 1 -type d | head -1)"
    [ -n "$sub" ] && [ -f "$sub/manifest.json" ] && cp -a "$sub"/. "$ROOT/extension/" && rm -rf "$sub"
  fi
}

# Backwards compatible bare-path fallback to a local clone if nothing was set.
if [ -z "${CEKI_EXT_DIST:-}" ] && [ -z "${CEKI_EXT_URL:-}" ] && [ -z "${CEKI_EXT_ZIP:-}" ] && [ -z "${CEKI_EXT_CRX:-}" ] \
   && [ ! -f "$EXT_SRC/manifest.json" ] && [ ! -f "$EXT_SRC" ] && [ "$EXT_KIND" = "dir" ]; then
  for cand in "$ROOT/../browser-extension/dist" "$HOME/browser-extension/dist"; do
    if [ -f "$cand/manifest.json" ]; then
      echo "[ceki-provider] using local extension clone: $cand"
      EXT_SRC="$cand"; EXT_KIND="dir"
      break
    fi
  done
fi

stage_ext "$EXT_SRC" "$EXT_KIND"

echo "[ceki-provider] building image: $IMAGE"
docker build -t "$IMAGE" -f "$ROOT/Dockerfile" "$ROOT"

echo "[ceki-provider] done: $IMAGE"
echo "  run:  docker run --rm -e CEKI_PROVIDER_TOKEN=<token> $IMAGE"
