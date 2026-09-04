#!/bin/sh
# Ceki headless-provider container entrypoint.
#
# Starts a virtual display (Xvfb) if none is already running, then execs the
# real command. `exec` is important: the provider process replaces this script
# and becomes PID 1, so `docker stop` (SIGTERM to PID 1) reaches the provider
# directly and the browser session is shut down cleanly (rented browser goes
# offline, no orphaned processes).
#
# DISPLAY can be overridden by the user (e.g. to attach to an external X
# server / x11vnc).

set -e

# Inherit a non-UTC timezone so the provider browser matches the IP geolocation.
# Priority: TZ env (compose passes ${TZ:-}) → /etc/timezone.
if [ -z "${TZ:-}" ] && [ -f /etc/timezone ]; then
  TZ="$(cat /etc/timezone)"
  export TZ
fi

if [ -z "${DISPLAY:-}" ]; then
  export DISPLAY=:99
fi

# --- Extension config --------------------------------------------------------
# The extension is static JS: it cannot read container env, so the environment
# is baked into the built bundle at build time. The image ships with the PROD
# URLs and defaults to prod.
# CEKI_WS_URL / CEKI_API_URL (not part of the public contract) override the
# baked-in URLs for internal non-prod runs.

EXT_DIR="${CEKI_PROVIDER_EXT_DIR:-/opt/ceki/extension}"
DEFAULT_WS_URL="wss://browser.ceki.me/ws/provider"
DEFAULT_API_URL="https://api.ceki.me"

# Stable extension id (derived from the public manifest key). Used for the
# external-update policy file Chrome reads at start.
EXT_ID="gfionhbdkojjnjpbhlblopoaecdpllhb"

# --- External-extension policy -------------------------------------------------
# Chrome installs and auto-updates the extension itself from an update channel
# when a policy file /usr/share/chromium/extensions/<id>.json exists. This is
# the primary extension source: Chrome checks the channel every few hours and
# updates the running extension without restarting the container — so even a
# provider that runs for months stays on the latest build. The unpacked
# --load-extension copy below is only a fallback (offline / local runs).
#   CEKI_EXT_POLICY_URL    updates.xml for the policy (default: prod /ext/updates.xml)
#   CEKI_EXT_UPDATE_URL    CRX URL for the unpacked fallback (default: prod latest.crx)
#   CEKI_EXT_SKIP_UPDATE=1 do not write the policy (unpacked only)
EXT_POLICY_URL="${CEKI_EXT_POLICY_URL:-https://browser.ceki.me/ext/updates.xml}"
write_external_policy() {
  # Explicit skip, or a relay URL override → do not write the policy: Chrome
  # would fetch the build from the channel with its own relay URL baked in,
  # bypassing the override. Those cases keep the unpacked --load-extension path
  # + URL patching below. CEKI_API_URL alone does NOT disable the policy — the
  # provider handshake can target any API while the extension build comes from
  # the channel.
  if [ -n "${CEKI_EXT_SKIP_UPDATE:-}" ]; then
    echo "[ceki-provider] extension: external policy disabled (CEKI_EXT_SKIP_UPDATE=1)"
    return 0
  fi
  if [ -n "${CEKI_WS_URL:-}" ] && [ "$CEKI_WS_URL" != "$DEFAULT_WS_URL" ]; then
    echo "[ceki-provider] extension: CEKI_WS_URL override — unpacked patching mode, no external policy"
    return 0
  fi
  policy_dir="/usr/share/chromium/extensions"
  mkdir -p "$policy_dir"
  printf '{"external_update_url":"%s"}\n' "$EXT_POLICY_URL" > "$policy_dir/$EXT_ID.json"
  echo "[ceki-provider] extension: external policy -> $policy_dir/$EXT_ID.json ($EXT_POLICY_URL)"
}

# --- Extension auto-update from the release channel ---------------------------
# At every container start we fetch the latest extension CRX from the update
# channel and unpack it over EXT_DIR, so the provider always runs the newest
# build without a rebuild. On any failure (offline, bad archive) the copy baked
# into the image at build time is kept — a provider always starts.
#   CEKI_EXT_UPDATE_URL      channel to fetch from (default: prod /ext/)
#   CEKI_EXT_SKIP_UPDATE=1   disable auto-update (offline / local test)
EXT_UPDATE_URL="${CEKI_EXT_UPDATE_URL:-https://browser.ceki.me/ext/ceki-browser-extension-latest.crx}"

update_extension() {
  [ -d "$EXT_DIR" ] || mkdir -p "$EXT_DIR"
  if [ -n "${CEKI_EXT_SKIP_UPDATE:-}" ]; then
    echo "[ceki-provider] extension: auto-update disabled (CEKI_EXT_SKIP_UPDATE=1)"
    return 0
  fi
  [ -n "$EXT_UPDATE_URL" ] || return 0

  UPD_TMP="$(mktemp)"
  if ! curl -fsSL --connect-timeout 10 --max-time 90 "$EXT_UPDATE_URL" -o "$UPD_TMP" 2>/dev/null; then
    echo "[ceki-provider] extension: update download failed, keeping baked copy" >&2
    rm -f "$UPD_TMP"
    return 0
  fi

  UPD_DIR="$(mktemp -d)"
  if ! python3 -c "import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$UPD_TMP" "$UPD_DIR" 2>/dev/null; then
    echo "[ceki-provider] extension: unpack failed, keeping baked copy" >&2
    rm -f "$UPD_TMP"; rm -rf "$UPD_DIR"
    return 0
  fi

  # The archive may wrap everything in a single top-level dir — flatten it.
  if [ ! -f "$UPD_DIR/manifest.json" ]; then
    UPD_SUB="$(find "$UPD_DIR" -mindepth 1 -maxdepth 1 -type d | head -1)"
    if [ -n "$UPD_SUB" ] && [ -f "$UPD_SUB/manifest.json" ]; then
      mv "$UPD_SUB"/* "$UPD_DIR"/ 2>/dev/null
      rm -rf "$UPD_SUB"
    fi
  fi

  if [ ! -f "$UPD_DIR/manifest.json" ]; then
    echo "[ceki-provider] extension: no manifest in update, keeping baked copy" >&2
    rm -f "$UPD_TMP"; rm -rf "$UPD_DIR"
    return 0
  fi

  rm -rf "$EXT_DIR"
  mv "$UPD_DIR" "$EXT_DIR"
  rm -f "$UPD_TMP"
  echo "[ceki-provider] extension: updated from $EXT_UPDATE_URL"
}

# Primary path: write the external policy so Chrome installs + auto-updates the
# extension itself. The unpacked download below only keeps the baked copy fresh
# as a fallback when the external channel is unreachable (offline / local runs).
write_external_policy
update_extension

patch_extension_urls() {
  [ -d "$EXT_DIR" ] || return 0
  files="$(grep -rlE "${DEFAULT_WS_URL}|${DEFAULT_API_URL}" "$EXT_DIR" --include='*.js' 2>/dev/null || true)"
  [ -n "$files" ] || return 0
  if [ -n "${CEKI_WS_URL:-}" ] && [ "$CEKI_WS_URL" != "$DEFAULT_WS_URL" ]; then
    for f in $files; do
      sed -i "s|${DEFAULT_WS_URL}|${CEKI_WS_URL}|g" "$f"
    done
    echo "[ceki-provider] extension: relay WS  ${DEFAULT_WS_URL} -> ${CEKI_WS_URL}"
  fi
  if [ -n "${CEKI_API_URL:-}" ] && [ "$CEKI_API_URL" != "$DEFAULT_API_URL" ]; then
    for f in $files; do
      sed -i "s|${DEFAULT_API_URL}|${CEKI_API_URL}|g" "$f"
    done
    echo "[ceki-provider] extension: api URL  ${DEFAULT_API_URL} -> ${CEKI_API_URL}"
  fi
}

patch_extension_urls

# --- Viewport / Xvfb screen ---------------------------------------------------
# CEKI_PROVIDER_VIEWPORT (WxH, default 1920x1080) drives both the Chromium
# window size / viewport (app.py) and the Xvfb framebuffer, so the virtual
# screen always matches the rendered resolution. Unparseable values fall
# back to the default.
CEKI_PROVIDER_VIEWPORT="${CEKI_PROVIDER_VIEWPORT:-1920x1080}"
case "$CEKI_PROVIDER_VIEWPORT" in
  *x*) ;;
  *) CEKI_PROVIDER_VIEWPORT=1920x1080 ;;
esac
XVFB_W="${CEKI_PROVIDER_VIEWPORT%x*}"
XVFB_H_RAW="${CEKI_PROVIDER_VIEWPORT#*x}"
if ! echo "$XVFB_W" | grep -qE '^[0-9]+$' || ! echo "$XVFB_H_RAW" | grep -qE '^[0-9]+$'; then
  echo "[ceki-provider] WARN: CEKI_PROVIDER_VIEWPORT='${CEKI_PROVIDER_VIEWPORT}' invalid, using 1920x1080" >&2
  XVFB_W=1920
  XVFB_H_RAW=1080
fi
XVFB_H="$XVFB_H_RAW"
export CEKI_PROVIDER_VIEWPORT="${XVFB_W}x${XVFB_H_RAW}"

# Start Xvfb if it is not already up on our display.
# Clear stale lock/socket first: on `docker restart` the writable layer keeps
# /tmp/.X99-lock from the previous (killed) Xvfb, the new one refuses to start
# ("If this server is no longer running, remove /tmp/.X99-lock"), and chromium
# then dies with "Missing X server" -> crash-loop.
if ! xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
  echo "[ceki-provider] starting Xvfb on ${DISPLAY} (screen ${XVFB_W}x${XVFB_H}x24)"
  rm -f "/tmp/.X${DISPLAY#:}-lock" "/tmp/.X11-unix/X${DISPLAY#:}"
  Xvfb "${DISPLAY}" -screen 0 "${XVFB_W}x${XVFB_H}x24" -nolisten tcp >/tmp/xvfb.log 2>&1 &
fi

# --- Browser check ------------------------------------------------------------
# Playwright installs its own pinned Chromium build. Verify it is present and,
# if the pinned build changed (image rebuilt with a newer playwright), fetch it.
# Disable with CEKI_SKIP_BROWSER_UPDATE=1 (e.g. air-gapped / local test).
if [ -z "${CEKI_SKIP_BROWSER_UPDATE:-}" ] && command -v python3 >/dev/null 2>&1; then
  if ! python3 -m playwright install chromium >/dev/null 2>&1; then
    echo "[ceki-provider] browser: playwright install check failed (continuing with baked build)" >&2
  fi
fi

exec "$@"
