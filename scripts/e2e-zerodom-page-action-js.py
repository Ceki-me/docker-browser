#!/usr/bin/env python3
"""
E2E: js-sdk (@ceki/sdk) zerodom page()/act() against a LIVE provider.

The js twin of e2e-zerodom-page-action.py. The js-sdk browser entry
(dist/browser.js) does NOT export the agent-token `connect` (only
connectAsUserSession), so this driver uses the **node** entry (dist/index.js)
with connect(agentToken) — the exact same contract the python e2e uses:

  1. page()           -> non-empty interaction graph (nodes + compact text)
  2. act('fill[N]')   -> really writes the value (verified via raw CDP reading
                         el.value — not the SDK's own claim)
  3. act('click[N]')  -> navigates (URL changes) + graph diff
  4. screenshot()     -> old flow still works after page/act round-trips

Runs a Node driver (scripts/e2e-zerodom-page-action-js-driver.mjs) that imports
the js-sdk dist and rents with token rotation.

Run (live infra, dev contour):
  RENTER_AGENT_TOKENS='tok1,tok2' \
  CEKI_RELAY_URL_DEV=wss://browser.ittribe.org/ws/agent \
  CEKI_API_URL_DEV=https://clawapi.ittribe.org \
  python3 scripts/e2e-zerodom-page-action-js.py [schedule] [iterations]

Optional:
  CEKI_JS_DIST=/abs/path/to/js-sdk/dist   (default ../../js-sdk/dist)
"""
import os
import subprocess
import sys

RELAY = os.environ.get("CEKI_RELAY_URL_DEV", "wss://browser.ittribe.org/ws/agent")
API = os.environ.get("CEKI_API_URL_DEV", "https://clawapi.ittribe.org")
TOKENS = [t.strip() for t in os.environ.get("RENTER_AGENT_TOKENS", "").split(",") if t.strip()]
DIST = os.environ.get("CEKI_JS_DIST", os.path.join(os.path.dirname(__file__), "..", "..", "js-sdk", "dist"))
DRIVER = os.environ.get(
    "CEKI_JS_E2E_DRIVER",
    os.path.join(os.path.dirname(__file__), "e2e-zerodom-page-action-js-driver.mjs"),
)


def main() -> None:
    if not TOKENS:
        print("FATAL: RENTER_AGENT_TOKENS unset (live infra only)")
        sys.exit(2)
    if not os.path.exists(os.path.join(DIST, "index.js")):
        print(f"FATAL: js-sdk dist not found: {DIST}/index.js (build js-sdk first: npm run build)")
        sys.exit(2)
    if not os.path.exists(DRIVER):
        print(f"FATAL: node driver not found: {DRIVER}")
        sys.exit(2)
    sid = sys.argv[1] if len(sys.argv) > 1 else "40693"
    iters = sys.argv[2] if len(sys.argv) > 2 else "2"
    env = dict(os.environ)
    env.update({
        "CEKI_RELAY_URL_DEV": RELAY,
        "CEKI_API_URL_DEV": API,
        "RENTER_AGENT_TOKENS": ",".join(TOKENS),
        "CEKI_JS_INDEX": os.path.join(DIST, "index.js"),
    })
    # run the node driver in this process's terminal (foreground)
    rc = subprocess.run(
        ["node", DRIVER, sid, iters], env=env, text=True
    ).returncode
    sys.exit(rc)


if __name__ == "__main__":
    main()