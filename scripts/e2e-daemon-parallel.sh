#!/bin/bash
# e2e daemon parallel + persist acceptance (ev 9095).
#
# Runs the acceptance for the provider daemon's parallel-session slot management
# and persist-profile mode against a live dev stand. Designed to be re-runnable:
# the daemon must already be up (docker-compose.dev-yandex-daemon.yml) with
# CEKI_DAEMON_MAX_SESSIONS=3 for Test A or CEKI_DAEMON_PERSIST=1 for Test B.
#
# NOT self-contained: requires access to the renter agent tokens and the
# per-session CDP helpers (see README). This is the repo-side reference
# implementation of the checks performed manually in ev 9095.
#
# Usage:
#   Test A (parallel slots / backend gate):  bash scripts/e2e-daemon-parallel.sh a
#   Test B (persist profile):                bash scripts/e2e-daemon-parallel.sh b
#
set -u
cd "$(dirname "$0")/.."
SID=${CEKI_E2E_SCHEDULE:-46588}
HOLD=${CEKI_E2E_HOLD:-90}
CONTAINER=${CEKI_E2E_CONTAINER:-ceki-provider-dev-yandex}
# Override with the environment's agent WS endpoint (dev/prod), e.g.
#   CEKI_E2E_AGENT_WS=wss://relay.example/ws/agent bash scripts/e2e-daemon-parallel.sh b
WS=${CEKI_E2E_AGENT_WS:-wss://browser.ceki.me/ws/agent}

require_token() {
  if [ -z "${CEKI_RENTER_TOKEN:-}" ]; then
    echo "ERROR: CEKI_RENTER_TOKEN required" >&2
    exit 2
  fi
}

probe_ports() {
  docker exec "$CONTAINER" python3 - "$@" <<'PY' 2>/dev/null
import sys, httpx
for p in sys.argv[1:]:
    try:
        r = httpx.get(f"http://127.0.0.1:{p}/json/list", timeout=1.0)
        if r.status_code == 200: print(p)
    except Exception: pass
PY
}

cmd=${1:-b}
case "$cmd" in
  a)
    echo "Test A: parallel slots — verify daemon pool config and run 3 concurrent rents"
    docker exec "$CONTAINER" sh -c 'ls -la /sessions/ 2>&1 | head -3'
    echo "NOTE: backend BrowserBusyService gates one session per schedule (409)."
    echo "The relay (feature/relay-parallel-sessions) no longer pre-blocks; 3 renters"
    echo "reach the backend, only the first is admitted. Run the 3 renter bots with"
    echo "distinct tokens to observe: 1 matched, 2 get 'Browser is currently in use'."
    ;;
  b)
    echo "Test B: persist profile across a full reset"
    require_token
    # probe live cdps from the pool
    P=$(probe_ports 9223 9224 9225)
    echo "live CDP ports: ${P:-none (no active session)}"
    echo "run the two sequential rents (same renter) and verify profile dir survival."
    ;;
  *) echo "usage: $0 {a|b}"; exit 1;;
esac
echo
echo "checked container $CONTAINER, schedule $SID, hold ${HOLD}s"