#!/usr/bin/env bash
#
# P2 e2e detector — extension presence anomalies on a live provider.
#
# Watches a real provider container's daemon log for two failure signatures
# observed during sequential live rent cycles:
#
#   A) 0-connect:      a session got `match` but its extension never reached
#                      the presence-WS within the relay window (~39s)
#                      → relay `session_ended: instance N not connected yet`
#   B) double-connect: a session's extension connected to the local-ws twice
#                      (second bind races / breaks the session's navigate)
#
# Usage (live infra, after running a rent cycle against the provider):
#   scripts/e2e-presence-anomaly.sh <container> [--since 6h]
#
# Prints per-session connect counts and flags the two anomaly signatures.
# Exit 0 = no anomalies found in window; 1 = anomalies present.
set -uo pipefail

CONTAINER="${1:?usage: e2e-presence-anomaly.sh <container> [--since N]}"
SINCE="6h"
if [ "${2:-}" = "--since" ]; then SINCE="${3:-6h}"; fi

LOG="$(docker logs "$CONTAINER" --since "$SINCE" 2>&1)"

echo "=== P2 presence-anomaly scan: $CONTAINER (since $SINCE) ==="

# Signature A: relay gave up on a session whose extension never connected.
ZERO_CONNECTS="$(
  echo "$LOG" \
    | grep -E "relay -> session_ended: instance [0-9]+ not connected yet, dropping" \
    | sed -E 's/.*instance ([0-9]+) not connected.*/\1/' \
    | sort -u
)"
if [ -n "$ZERO_CONNECTS" ]; then
  echo "  [FAIL] 0-connect sessions (extension never reached presence-WS):"
  echo "$ZERO_CONNECTS" | sed 's/^/         sid /'
else
  echo "  [ok]   no 0-connect sessions"
fi

# Signature B: sessions whose extension connected more than once.
DOUBLE="$(
  echo "$LOG" \
    | grep -oE 'extension connected for session [0-9]+' \
    | sed -E 's/.*session ([0-9]+)/\1/' \
    | sort \
    | uniq -c \
    | awk '$1 > 1 {print $2" (x"$1")"}'
)"
if [ -n "$DOUBLE" ]; then
  echo "  [FAIL] double-connect sessions:"
  echo "$DOUBLE" | sed 's/^/         sid /'
else
  echo "  [ok]   no double-connect sessions"
fi

# Summary of all connect counts for context.
echo "--- all sessions (match window) ---"
echo "$LOG" \
  | grep -oE 'relay-in: type=match sid=[0-9]+' \
  | sed -E 's/.*sid=([0-9]+)/\1/' \
  | sort -u \
  | while read -r sid; do
      n="$(echo "$LOG" | grep -cE "extension connected for session $sid\b")"
      printf "  sid %-6s connects=%s\n" "$sid" "$n"
    done \
  | tail -40

if [ -n "$ZERO_CONNECTS" ] || [ -n "$DOUBLE" ]; then
  echo "=== P2 result: FAIL (anomalies present) ==="
  exit 1
fi
echo "=== P2 result: PASS ==="
exit 0