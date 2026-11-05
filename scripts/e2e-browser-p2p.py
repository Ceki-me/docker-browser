#!/usr/bin/env python3
"""
E2E browser test — @ceki/sdk/browser build driven from a REAL browser (Playwright).

What it proves: the JS SDK's *browser* entry (`dist/browser.js`) can open a P2P
(WebRTC) rental against a live provider the same way the web front does:
  1. create a user session server-side (POST /api/sessions as the logged-in user)
  2. connect the SDK client via connectAsUserSession(renterToken, ws_url)
  3. rent(schedule) -> relay matches the pre-created session
  4. assert the P2P ceki-cmd data channel actually opens and CDP flows over it
     (navigate / evaluate / click / screenshot round-trip through P2P, not WS).

This is the browser twin of the Node e2e scripts in this dir — but the SDK is
loaded inside a real Chromium page via Playwright, so it exercises the native
WebSocket + RTCPeerConnection transport twins (ws-browser.ts / webrtc-browser.ts)
that the web front ships.

No internal endpoints/tokens are hardcoded — everything comes from the
environment. See "Run" below.

Run (live infra — set your contour's API/relay):
  # credentials: either a ready user token ...
  USER_TOKEN=<renter user token> \
  # ... or a login pair to mint one at runtime (POST /api/login):
  CEKI_USER_EMAIL=... CEKI_USER_PASSWORD=... \
  # endpoints (required; point at the contour hosting BROWSER_ID)
  CEKI_API_URL=https://api.example.com/api \
  CEKI_RELAY_URL=wss://relay.example.com/ws/user \
  # which schedule to rent + how many iterations
  BROWSER_ID=48475 ITERATIONS=3 \
  python3 scripts/e2e-browser-p2p.py

Optional:
  CEKI_SDK_DIST=/abs/path/to/dist/browser.js   (default: repo-local js-sdk dist)
  HEADED=1            run Playwright with a visible browser
  P2P_TIMEOUT=20000   ms to wait for the cmd data channel (default 15000)
"""
import asyncio
import json
import os
import sys

# playwright must be importable; the SDK dist path is resolved from env
try:
    from playwright.async_api import async_playwright
except ImportError:
    print("FATAL: pip install playwright (and `playwright install chromium`)")
    sys.exit(2)


def env(name: str, default: str = "", required: bool = False) -> str:
    v = os.environ.get(name, default)
    if required and not v:
        print(f"FATAL: {name} must be set (see script header)")
        sys.exit(2)
    return v


USER_TOKEN = env("USER_TOKEN")
CEKI_EMAIL = env("CEKI_USER_EMAIL")
CEKI_PASSWORD = env("CEKI_USER_PASSWORD")
# Contour endpoints are NOT hardcoded — the public repo must not reference
# internal hosts. The defaults here are empty on purpose; every runner must
# pass their own CEKI_API_URL / CEKI_RELAY_URL. (Matches the "no internal URLs
# in public repos" rule.)
API_URL = env("CEKI_API_URL", "")
RELAY_URL = env("CEKI_RELAY_URL", "")
if not API_URL or not RELAY_URL:
    print("FATAL: CEKI_API_URL and CEKI_RELAY_URL must be set (no internal defaults in public repo)")
    sys.exit(2)
BROWSER_ID = int(env("BROWSER_ID", "48475"))
ITERATIONS = int(env("ITERATIONS", "1"))
SDK_DIST = env(
    "CEKI_SDK_DIST",
    os.path.join(
        os.path.dirname(__file__), "..", "..", "js-sdk", "dist", "browser.js"
    ),
)
P2P_TIMEOUT = int(env("P2P_TIMEOUT", "15000"))
HEADED = env("HEADED", "") != ""

# A tiny HTML page that loads the SDK bundle and exposes a promise-based driver
# to the Playwright side. All logic runs in-page; we only await results.
PAGE_SNIPPET = r"""
(async () => {
  const out = {};
  try {
    // 1. token: prefer USER_TOKEN, else login
    let token = __USER_TOKEN__;
    if (!token) {
      const lr = await fetch(__API_URL__ + '/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ email: __EMAIL__, password: __PASSWORD__ }),
      });
      const ld = await lr.json();
      token = (ld.user && ld.user.token) || ld.token;
      if (!token) throw new Error('login failed: ' + JSON.stringify(ld).slice(0, 200));
    }
    out.tokenMinted = !!token;

    // 2. create a user session (POST /api/sessions)
    const sr = await fetch(__API_URL__ + '/sessions', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ browser_id: __BROWSER_ID__, mode: 'incognito' }),
    });
    const sd = await sr.json();
    if (!sd.session_id) throw new Error('session create failed: ' + JSON.stringify(sd).slice(0, 300));
    out.sessionId = sd.session_id;

    // 3. connect the SDK as this user renter
    const client = await CekiSDK.connectAsUserSession({
      renterToken: sd.renter_token,
      relayUrl: sd.ws_url || __RELAY_URL__,
      apiUrl: __API_URL__,
    });
    out.wsConnected = true;

    // 4. rent the schedule -> P2P transport should initialize and open cmd DC
    const t0 = Date.now();
    const browser = await client.rent(__BROWSER_ID__, {});
    out.rentMs = Date.now() - t0;
    out.sessionId = browser.sessionId || out.sessionId;

    // 5. wait for the ceki-cmd data channel
    const p2p = client._p2p;
    out.p2pInitialized = !!p2p;
    let dcOpen = false;
    if (p2p && typeof p2p.waitForDcOpen === 'function') {
      try {
        await Promise.race([
          p2p.waitForDcOpen(),
          new Promise((_, rej) => setTimeout(() => rej(new Error('dc timeout')), __P2P_TIMEOUT__)),
        ]);
        dcOpen = p2p.cmdDcOpen === true;
      } catch (e) { out.dcWaitError = String(e.message || e); }
    }
    out.cmdDcOpen = dcOpen;

    // If the DC did not come up, snapshot the transport internals so we can
    // localize WHERE the handshake broke: PC state / ICE state / cmd DC state /
    // pending remote ICE candidates / local DTLS fingerprint. Read through
    // bracket access so TS `private` in the SDK source doesn't matter at
    // runtime (these are plain JS fields).
    if (!dcOpen && p2p) {
      const t = p2p;
      out.p2pDiag = {
        pcState: t._pc ? t._pc.connectionState : null,
        iceState: t._pc ? t._pc.iceConnectionState : null,
        cmdDcState: t._cmdDc ? t._cmdDc.readyState : null,
        captureDcState: t._captureDc ? t._captureDc.readyState : null,
        pendingIce: Array.isArray(t._pendingRemoteCandidates) ? t._pendingRemoteCandidates.length : null,
        localFingerprint: t._localFingerprint || null,
        iceServers: (t._iceServers || []).map(s => s.urls),
      };
    }

    // The whole point is P2P. If the cmd DC did NOT come up, skip the CDP
    // round-trips — without it they fall to the WS path and can stall this
    // iteration for minutes. Report immediately; verdict() fails on
    // cmdDcOpen !== true.
    if (!dcOpen) {
      try { await browser.close(); } catch (e) {}
      try { await client.close(); } catch (e) {}
      out.ok = true;
      return out;
    }

    // 6. CDP round-trips. DC is open, so navigate / evaluate / screenshot all
    //    go over P2P (sendInput/send prefer the DC).
    try {
      await browser.navigate('https://example.com', 30000);
      out.navigateOk = true;
    } catch (e) { out.navigateError = String(e.message || e).slice(0, 200); }

    try {
      const res = await browser.send({ method: 'Runtime.evaluate', params: { expression: 'document.title' } }, 30000);
      out.evalTitle = JSON.stringify(res).slice(0, 200);
    } catch (e) { out.evalError = String(e.message || e).slice(0, 200); }

    // Latency probe: measure a click round-trip (mousePressed + mouseReleased,
    // each a CDP send over the DC) and a plain send. The user reported clicks
    // arriving with huge delay even when P2P is up — these numbers localize
    // whether the delay is in the DC round-trip itself or upstream.
    try {
      const t1 = Date.now();
      await browser.click(100, 100, { human: false });
      out.clickMs = Date.now() - t1;
    } catch (e) { out.clickError = String(e.message || e).slice(0, 200); }

    // Custom canvas-style input ({type:'mouse'}) — exactly what the web UI
    // sends via sendInput. Verifies this path lands in the BROWSER transport
    // (the Node SDK in e2e probes was losing these; see investigation).
    try {
      const t3 = Date.now();
      await browser.send({ method: 'Runtime.evaluate', params: { expression: 'window.__c2=0; document.addEventListener("click",()=>{window.__c2++;}); "ok"' } }, 30000);
      await browser.sendInput({ type: 'mouse', action: 'click', x: 200, y: 200, button: 0, clickCount: 1 });
      // poll briefly for landing
      let landed = false;
      for (let i = 0; i < 10; i++) {
        await new Promise(r => setTimeout(r, 300));
        const r = await browser.send({ method: 'Runtime.evaluate', params: { expression: 'window.__c2||0' } }, 15000);
        const v = r?.result?.result?.value ?? 0;
        if (v >= 1) { landed = true; break; }
      }
      out.sendInputLanded = landed;
      out.sendInputMs = Date.now() - t3;
    } catch (e) { out.sendInputError = String(e.message || e).slice(0, 200); }

    try {
      const t2 = Date.now();
      await browser.send({ method: 'Runtime.evaluate', params: { expression: '1+1' } }, 30000);
      out.evalMs = Date.now() - t2;
    } catch (e) { out.eval2Error = String(e.message || e).slice(0, 200); }

    try {
      const shot = await browser.screenshot({});
      const data = shot && typeof shot.data === 'string' ? shot.data : '';
      out.screenshotBytes = data.length;
    } catch (e) { out.shotError = String(e.message || e).slice(0, 200); }

    // cleanup: close browser + client
    try { await browser.close(); } catch (e) {}
    try { await client.close(); } catch (e) {}

    out.ok = true;
  } catch (e) {
    out.ok = false;
    out.error = String((e && e.message) || e).slice(0, 500);
  }
  return out;
})()
"""


def build_page_source() -> str:
    def jsstr(s: str) -> str:
        return json.dumps(s or "")

    src = PAGE_SNIPPET
    src = src.replace("__USER_TOKEN__", jsstr(USER_TOKEN))
    src = src.replace("__API_URL__", jsstr(API_URL))
    src = src.replace("__EMAIL__", jsstr(CEKI_EMAIL))
    src = src.replace("__PASSWORD__", jsstr(CEKI_PASSWORD))
    src = src.replace("__RELAY_URL__", jsstr(RELAY_URL))
    src = src.replace("__BROWSER_ID__", str(BROWSER_ID))
    src = src.replace("__P2P_TIMEOUT__", str(P2P_TIMEOUT))
    return src


async def run_once(idx: int) -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not HEADED)
        page = await browser.new_page()

        # The SDK browser bundle is ESM — a classic <script> would fail on the
        # `export` statement and leave nothing on globalThis. Inject it as a
        # <script type="module"> and append a footer that publishes the names
        # the driver needs onto window.CekiSDK (module bindings stay module-
        # scoped otherwise).
        with open(SDK_DIST, "r", encoding="utf-8") as f:
            bundle = f.read()
        bundle += (
            "\n"
            "globalThis.CekiSDK = { connectAsUserSession, Client, Browser, "
            "WebRTCTransport, CaptureChunkReassembler, errors: {"
            "CekiBrowserError, TransportError, TimeoutError } };\n"
        )
        await page.add_script_tag(content=bundle, type="module")
        # module scripts are deferred — wait until the footer has run
        try:
            await page.wait_for_function(
                "() => typeof window.CekiSDK !== 'undefined' && typeof window.CekiSDK.connectAsUserSession === 'function'",
                timeout=15000,
            )
        except Exception:
            await browser.close()
            return {"idx": idx, "error": "SDK module did not expose connectAsUserSession; check CEKI_SDK_DIST / browser bundle format"}

        # Capture console from the page — the SDK logs p2p handshake steps
        # (`[p2p-diag]`) there. When the cmd DC fails to open, these show
        # exactly where the handshake stalled (before createOffer / on
        # createOffer / on wsSend).
        console_log: list = []

        def _on_console(msg):
            text = msg.text or ""
            if "[p2p-diag]" in text or "[p2p-diag" in text:
                console_log.append(text)

        page.on("console", _on_console)

        # Budget per iteration: rent + dc wait (P2P_TIMEOUT) + a few CDP
        # round-trips. If any single step exceeds its budget, tear the page
        # down hard so the iteration cannot hang (asyncio.wait_for around a
        # Playwright await does NOT interrupt it).
        try:
            result = await asyncio.wait_for(
                page.evaluate(build_page_source()),
                timeout=15 + P2P_TIMEOUT / 1000 + 90,
            )
        except asyncio.TimeoutError:
            result = {"idx": idx, "error": "iteration budget exceeded (rent/dc/CDP stalled)"}
        except Exception as e:
            result = {"idx": idx, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        finally:
            try:
                # browser.close() can also stall on a wedged WebRTC peer — bound
                # it; pw.stop() (context exit) force-kills the driver process.
                await asyncio.wait_for(browser.close(), timeout=10)
            except Exception:
                try:
                    await pw.stop()
                except Exception:
                    pass
        if console_log:
            result = dict(result or {})
            result["p2pDiagLog"] = console_log
        return {"idx": idx, **(result or {})}


def verdict(r: dict) -> bool:
    if not r.get("ok"):
        return False
    # P2P must actually be up (cmd DC open) — this is the whole point of the test.
    if r.get("cmdDcOpen") is not True:
        return False
    # at least navigate + one CDP round-trip
    if not r.get("navigateOk"):
        return False
    if not r.get("evalTitle") and not r.get("screenshotBytes", 0):
        return False
    return True


async def main() -> None:
    if not (USER_TOKEN or (CEKI_EMAIL and CEKI_PASSWORD)):
        print("FATAL: set USER_TOKEN or CEKI_USER_EMAIL + CEKI_USER_PASSWORD (see header)")
        sys.exit(2)
    if not os.path.exists(SDK_DIST):
        print(f"FATAL: SDK dist not found at {SDK_DIST} (build js-sdk first, or set CEKI_SDK_DIST)")
        sys.exit(2)

    print(f"=== e2e browser-p2p: BROWSER_ID={BROWSER_ID} x{ITERATIONS} via {SDK_DIST} ===", flush=True)
    fails = 0
    for i in range(ITERATIONS):
        try:
            r = await asyncio.wait_for(run_once(i), timeout=180)
        except Exception as e:
            r = {"idx": i, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        ok = verdict(r)
        if not ok:
            fails += 1
        keys = ["sessionId", "rentMs", "p2pInitialized", "cmdDcOpen", "navigateOk", "evalTitle", "clickMs", "evalMs", "sendInputLanded", "sendInputMs", "screenshotBytes"]
        print(
            f"  {'PASS' if ok else 'FAIL'} #{i} " + " ".join(
                f"{k}={r.get(k)}" for k in keys if k in r
            ) + (f" err={r.get('error')}" if r.get("error") else "")
            + (f" diag={r.get('p2pDiag')}" if r.get("p2pDiag") else "")
            + (f" p2pLog={r.get('p2pDiagLog')}" if r.get("p2pDiagLog") else ""),
            flush=True,
        )
        await asyncio.sleep(4)
    print(f"=== e2e browser-p2p result: {'FAIL' if fails else 'PASS'} ({ITERATIONS - fails}/{ITERATIONS}) ===")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
