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

# --- Runtime environment override for the bundled extension config -----------
# The extension is static JS: it cannot read container env, so the environment
# is baked into the built bundle at build time.  The image ships with PROD URLs
# (wss://browser.ceki.me/ws/provider, https://api.ceki.me) and defaults to prod.
# To point the extension at another environment (e.g. the dev stand) without
# building a separate image, set CEKI_WS_URL / CEKI_API_URL: the full URL
# strings are replaced in the bundled JS before Chromium starts.  The provider
# launcher already reads CEKI_API_URL for its own API base (token handshake),
# so both stay consistent.
#
#   docker run -e CEKI_WS_URL=wss://browser.ittribe.org/ws/provider \
#              -e CEKI_API_URL=https://clawapi.ittribe.org \
#              -e CEKI_PROVIDER_TOKEN=<token> ceki/provider:latest

EXT_DIR="${CEKI_PROVIDER_EXT_DIR:-/opt/ceki/extension}"
DEFAULT_WS_URL="wss://browser.ceki.me/ws/provider"
DEFAULT_API_URL="https://api.ceki.me"

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

exec "$@"
