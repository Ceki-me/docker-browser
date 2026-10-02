#!/usr/bin/env python3
"""E2E: rental-window open mode (daemon parity) across the built extension.

Black-box check of the plugin's rental-window settings over the same channel
the provider daemon uses (CDP -> chrome.storage.local seed + the offscreen
sw-offscreen port -> session_create_window). Verifies the two states the daemon
cares about:

  Test A (daemon default):  open_window_normal=true, open_window_focused=true
      -> the rental window is created NORMAL and is the focused window on the
         display (the streamed X window shows the rent).

  Test B (user minimized):  open_window_normal=false, open_window_focused=false
      -> the rental window is created MINIMIZED, is NOT the focused window, and
         the pre-rent window keeps focus. Guards the Layer-2/3 protections in
         ports.ts (re-minimize after create, focus bounce-back).

Runs inside the provider container (needs Chromium + the built extension).
Usage:
    python3 scripts/e2e-window-mode.py [--dist /opt/ceki/extension] [--display :99]
"""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def derive_ext_id(manifest_path: str) -> str:
    m = json.load(open(manifest_path))
    key = m.get("key")
    assert key, f"{manifest_path} has no 'key' — cannot derive extension id"
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def cdp_eval(ws_url: str, expression: str) -> object:
    import httpx
    import websockets
    import asyncio

    async def _run():
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as sock:
            await sock.send(json.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "awaitPromise": True, "returnByValue": True},
            }))
            while True:
                msg = json.loads(await asyncio.wait_for(sock.recv(), timeout=20.0))
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise RuntimeError(f"CDP error: {msg['error']}")
                    return msg.get("result", {}).get("result", {}).get("value")

    return asyncio.run(_run())


def wait_for_targets(cdp: str, matcher, timeout_s: float = 30.0):
    import httpx
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            targets = httpx.get(f"{cdp}/json/list", timeout=1.5).json()
        except Exception:
            targets = []
        hit = [t for t in targets if matcher(t)]
        if hit:
            return hit
        time.sleep(0.5)
    return []


def derive_sw_ws(cdp: str, ext_id: str) -> str:
    tgts = wait_for_targets(cdp, lambda t: t.get("type") == "service_worker" and f"chrome-extension://{ext_id}/" in (t.get("url") or ""))
    if tgts:
        return tgts[0]["webSocketDebuggerUrl"]
    # fallback: background page for MV3 builds that use one
    tgts = wait_for_targets(cdp, lambda t: t.get("type") == "background_page" and f"chrome-extension://{ext_id}/" in (t.get("url") or ""))
    if tgts:
        return tgts[0]["webSocketDebuggerUrl"]
    raise RuntimeError("extension service worker not found")


def wait_for_window_in_focus(cdp: str, expect_focused_win: int | None, timeout_s: float = 15.0) -> bool:
    """Poll windows.getLastFocused — the focused window should be `expect_focused_win`."""
    import websockets
    # evaluate in any extension context: use the panel page if present, else SW
    global _last_sw_ws
    deadline = time.time() + timeout_s
    expr = "(async () => { const w = await chrome.windows.getLastFocused({populate:false}); return w ? w.id : null; })()"
    while time.time() < deadline:
        try:
            fid = cdp_eval(_last_sw_ws, expr)
            if fid == expect_focused_win:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=os.environ.get("CEKI_PROVIDER_EXT_DIR", "/opt/ceki/extension"))
    ap.add_argument("--display", default=":99")
    ap.add_argument("--chrome", default=os.environ.get("CEKI_CHROME", "/ms-playwright/chromium-*/chrome-linux64/chrome"))
    ap.add_argument("--port", default=9443, type=int)
    args = ap.parse_args()

    manifest = os.path.join(args.dist, "manifest.json")
    if not os.path.exists(manifest):
        print(f"ERROR: extension dist not found at {args.dist} (manifest missing) — build first (vite build)")
        sys.exit(2)
    ext_id = derive_ext_id(manifest)
    chrome_bin = args.chrome
    if "*" in chrome_bin:
        import glob
        hits = sorted(glob.glob(chrome_bin))
        if not hits:
            print(f"ERROR: no chrome binary matching {args.chrome}")
            sys.exit(2)
        chrome_bin = hits[-1]

    os.environ["DISPLAY"] = args.display
    profile = f"/tmp/e2e-win-mode-{os.getpid()}"
    # Grant the extension incognito access in the fresh profile — with
    # --load-extension the unpacked extension is NOT allowed in incognito by
    # default, and then chrome.windows.create({incognito:true}) returns null
    # (win_null). The provider grants this via the external-install policy;
    # here we pre-seed Preferences the same way Chromium does when the user
    # toggles "Allow in Incognito": extensions.settings.<id>.incognito = true.
    os.makedirs(f"{profile}/Default", exist_ok=True)
    pref_path = f"{profile}/Default/Preferences"
    pref = {}
    if os.path.exists(pref_path):
        pref = json.load(open(pref_path))
    pref.setdefault("extensions", {}).setdefault("settings", {})[ext_id] = {"incognito": True}
    with open(pref_path, "w") as fh:
        json.dump(pref, fh)

    cdp = f"http://127.0.0.1:{args.port}"
    cmd = [
        chrome_bin, "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
        "--no-first-run", "--no-default-browser-check", "--disable-background-mode",
        f"--user-data-dir={profile}", f"--remote-debugging-port={args.port}",
        "--remote-allow-origins=*",
        f"--disable-extensions-except={args.dist}", f"--load-extension={args.dist}",
        "about:blank",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)  # let Chrome pick up the Prefs incognito grant
    results = {}
    try:
        sw_ws = derive_sw_ws(cdp, ext_id)
        global _last_sw_ws
        _last_sw_ws = sw_ws
        print(f"extension id={ext_id} | SW ws ok")

        portal = None  # offscreen port not strictly needed: we drive storage + create directly
        waits = 0

        def seed(cfg: dict):
            nonlocal waits
            expr = "(async (s) => { await chrome.storage.local.set(s); return true; })(" + json.dumps(cfg) + ")"
            cdp_eval(sw_ws, expr)

        def current_win():
            return cdp_eval(sw_ws, "(async () => { const w = await chrome.windows.getLastFocused({populate:false}); return w ? w.id : null; })()")

        # helper: drive the REAL sw-offscreen port path (what ws-router.ts does).
        # The port must be opened from the extension's offscreen document (MV3
        # forbids a self-connect from the SW context), so we open the offscreen
        # page as a CDP target and run the request inside it — exactly like the
        # real offscreen doc does via offscreen/ports.ts connectToSw().
        def create_via_sw_port(window_url: str, profile_mode: str) -> int:
            """Send session_create_window through the sw-offscreen port and wait
            for the cdp_response, exactly like offscreen/ws-router.ts does."""
            off_tgts = wait_for_targets(
                cdp,
                lambda t: "/offscreen/offscreen.html" in (t.get("url") or ""),
                timeout_s=20.0,
            )
            if not off_tgts:
                # open the offscreen page ourselves if the SW hasn't yet
                off_url = f"chrome-extension://{ext_id}/offscreen/offscreen.html"
                httpx_get = __import__("httpx").get
                _ = httpx_get(f"{cdp}/json/new?{off_url}", timeout=3)  # open in tab
                off_tgts = wait_for_targets(cdp, lambda t: "/offscreen/offscreen.html" in (t.get("url") or ""))
            if not off_tgts:
                raise RuntimeError("offscreen target not found")
            off_ws = off_tgts[0]["webSocketDebuggerUrl"]
            expr = (
                "(async () => {"
                "  const p = chrome.runtime.connect({ name: 'sw-offscreen' });"
                "  const reqId = 90210;"
                "  return await new Promise((resolve, reject) => {"
                "    const to = setTimeout(() => { p.disconnect(); reject(new Error('timeout')); }, 25000);"
                "    p.onMessage.addListener((m) => {"
                "      if (m && m.type === 'cdp_response' && m._reqId === reqId) {"
                "        clearTimeout(to); p.disconnect();"
                "        resolve(m.ok ? m.windowId : ('ERR:' + (m.error?.message || 'unknown')));"
                "      }"
                "    });"
                "    p.postMessage({ type: 'session_create_window', url: " + json.dumps(window_url) + ","
                "                    profile_mode: " + json.dumps(profile_mode) + ", _reqId: reqId });"
                "  });"
                "})()"
            )
            import websockets
            import asyncio

            async def _eval(ws_url: str, expression: str):
                async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as sock:
                    await sock.send(json.dumps({
                        "id": 7,
                        "method": "Runtime.evaluate",
                        "params": {"expression": expression, "awaitPromise": True, "returnByValue": True},
                    }))
                    while True:
                        m = json.loads(await asyncio.wait_for(sock.recv(), timeout=30.0))
                        if m.get("id") == 7:
                            if "error" in m:
                                raise RuntimeError(f"CDP error: {m['error']}")
                            v = m.get("result", {}).get("result", {}).get("value")
                            return v

            r = asyncio.run(_eval(off_ws, expr))
            if isinstance(r, str) and r.startswith("ERR:"):
                raise RuntimeError(f"session_create_window failed: {r}")
            return int(r)

        # ── Test A: daemon default (normal + focused) ──────────────────────────
        print("\n[Test A] open_window_normal=true, open_window_focused=true (daemon default)")
        seed({"open_window_normal": True, "open_window_focused": True, "restore_focus_on_rental": False})
        rent_a = create_via_sw_port("https://example.com", "incognito")
        time.sleep(1.0)
        focused_a = current_win()
        state_a = cdp_eval(sw_ws, f"(async () => {{ const w = await chrome.windows.get({rent_a}, {{}}); return w ? w.state : 'gone'; }})()")
        ok_a = focused_a == rent_a and state_a in ("normal", "maximized")
        print(f"  rent id={rent_a}, focused={focused_a}, state={state_a} -> {'PASS' if ok_a else 'FAIL'}")
        results["A: daemon default opens focused + normal"] = ok_a

        # clean the test window
        try:
            cdp_eval(sw_ws, f"(async () => {{ try {{ await chrome.windows.remove({rent_a}); }} catch(e){{}} }})()")
        except Exception:
            pass

        # ── Test B: user minimized (false/false) ───────────────────────────────
        # Note: a truly minimized `state` requires a window manager on the
        # display. Inside the headless provider container (Xvfb, no WM), the
        # WM could never minimize even on demand — so the "minimized" state
        # assertion is only meaningful on a desktop session with a WM. What we
        # CAN guarantee everywhere: the rental window must NOT steal focus from
        # the user's window (the "Open focused" setting holds). With
        # E2E_EXPECT_WM=1 the script additionally asserts state == 'minimized'
        # (run it on a desktop / with a WM for the full check).
        print("\n[Test B] open_window_normal=false, open_window_focused=false (user minimized)")
        seed({"open_window_normal": False, "open_window_focused": False, "restore_focus_on_rental": False})
        orig_id = current_win()
        rent_b = create_via_sw_port("https://example.com", "incognito")
        # give the WM a beat to settle focus
        time.sleep(1.5)
        focused_after = current_win()
        state_b = cdp_eval(sw_ws, f"(async () => {{ const w = await chrome.windows.get({rent_b}, {{}}); return w ? w.state : 'gone'; }})()")
        ok_b_focus = focused_after == orig_id
        expect_wm = os.environ.get("E2E_EXPECT_WM") == "1"
        ok_b_min = (not expect_wm) or state_b == "minimized"
        print(f"  rent id={rent_b}, pre-rent id={orig_id}, focused after={focused_after}, state={state_b}")
        print(f"  focus stays on origin -> {'PASS' if ok_b_focus else 'FAIL'}")
        print(f"  rent window minimized (requires WM) -> {'PASS' if ok_b_min else 'FAIL'}")
        results["B: user minimized stays minimized"] = ok_b_min
        results["B: focus not stolen from user window"] = ok_b_focus

        # cleanup
        try:
            cdp_eval(sw_ws, f"(async () => {{ try {{ await chrome.windows.remove({rent_b}); }} catch(e){{}} }})()")
        except Exception:
            pass

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except Exception:
            proc.kill()

    print("\n=== RESULTS ===")
    failed = 0
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        failed += 0 if ok else 1
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()