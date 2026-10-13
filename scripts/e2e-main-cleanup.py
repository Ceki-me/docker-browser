#!/usr/bin/env python3
"""E2E: main-profile rent must NOT close the user's real windows.

Regression test for the 0.6.340 bug (9b30f44a / PR #49): `session_create_window`
with profile_mode="main" closed EVERY non-incognito window (chrome.windows.remove
on all of them). In a throwaway container profile that is harmless, but on a
host browser profile ("Ноут" 40693) a main rent destroyed the user's real
windows.

This script drives the REAL extension over the same channel the provider daemon
uses (CDP -> chrome.storage.local seed + the offscreen sw-offscreen port ->
session_create_window), opens a "user" window with a real site, a couple of
extension-owned windows (about:blank / panel), then creates a main-profile
rental window and asserts which windows survive:

  Test A (main rent, the bug):  user's real site window MUST survive.
      -> FAILS on 0.6.340 (bug), PASSES with PR #49 fix.
  Test B (main rent):           extension-owned windows (about:blank / panel)
      -> are closed (that is the intended cleanup inside a container).
  Test C (incognito rent):      NO window is touched.

Runs inside the provider container (needs Chromium + the built extension).
Usage:
    python3 scripts/e2e-main-cleanup.py [--dist /opt/ceki/extension] [--display :99]
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
    import asyncio
    import websockets

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
    tgts = wait_for_targets(cdp, lambda t: t.get("type") == "service_worker" and f"chrome-extension://{ext_id}/" in (t.get("url") or ""), timeout_s=60.0)
    if tgts:
        return tgts[0]["webSocketDebuggerUrl"]
    tgts = wait_for_targets(cdp, lambda t: t.get("type") == "background_page" and f"chrome-extension://{ext_id}/" in (t.get("url") or ""), timeout_s=60.0)
    if tgts:
        return tgts[0]["webSocketDebuggerUrl"]
    raise RuntimeError("extension service worker not found")


def is_ext_owned_url(url: str) -> bool:
    """Mirror of ports.ts isExtensionOwnedWindow — windows matching these are
    safe for the main-cleanup to close; everything else (user sites) is not."""
    if not url:
        return False
    if url == "about:blank":
        return True
    if url.startswith("chrome-extension://"):
        return True
    import re
    return bool(re.match(r"^https?://([a-z0-9.-]+\.)?ceki\.(me|com)/", url, re.I)
                or re.match(r"^https?://([a-z0-9.-]+\.)?ittribe\.org/", url, re.I))


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
    profile = f"/tmp/e2e-main-cleanup-{os.getpid()}"
    # Incognito grant for the unpacked extension (same as e2e-window-mode.py).
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
        "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={profile}", f"--remote-debugging-port={args.port}",
        "--remote-allow-origins=*",
        f"--disable-extensions-except={args.dist}", f"--load-extension={args.dist}",
        "about:blank",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    results = {}
    try:
        sw_ws = derive_sw_ws(cdp, ext_id)
        print(f"extension id={ext_id} | SW ws ok")

        def create_window(url: str, incognito: bool = False) -> int:
            expr = (
                "(async () => {"
                f"  const w = await chrome.windows.create({{ url: {json.dumps(url)}, incognito: {json.dumps(incognito)}, focused: false }});"
                "  return w ? w.id : null;"
                "})()"
            )
            wid = cdp_eval(sw_ws, expr)
            if wid is None:
                raise RuntimeError(f"chrome.windows.create failed for {url}")
            return int(wid)

        def window_urls() -> list:
            """[(winId, [tab urls])] for every window, including tabs."""
            expr = (
                "(async () => { const wins = await chrome.windows.getAll({populate: true});"
                "  return wins.map(w => ({ id: w.id, incognito: w.incognito, tabs: (w.tabs || []).map(t => t.url || '') })); })()"
            )
            return cdp_eval(sw_ws, expr) or []

        # helper: drive the REAL sw-offscreen port path (like ws-router.ts).
        def create_via_sw_port(window_url: str, profile_mode: str) -> int:
            off_tgts = wait_for_targets(cdp, lambda t: "/offscreen/offscreen.html" in (t.get("url") or ""), timeout_s=20.0)
            if not off_tgts:
                off_url = f"chrome-extension://{ext_id}/offscreen/offscreen.html"
                import httpx
                httpx.get(f"{cdp}/json/new?{off_url}", timeout=3)
                off_tgts = wait_for_targets(cdp, lambda t: "/offscreen/offscreen.html" in (t.get("url") or ""))
            if not off_tgts:
                raise RuntimeError("offscreen target not found")
            off_ws = off_tgts[0]["webSocketDebuggerUrl"]
            expr = (
                "(async () => {"
                "  const p = chrome.runtime.connect({ name: 'sw-offscreen' });"
                "  const reqId = 90211;"
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
            import asyncio
            import websockets

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

        def find_win(windows: list, url_pred, only_incognito=None):
            for w in windows:
                if only_incognito is not None and w["incognito"] != only_incognito:
                    continue
                if any(url_pred(u) for u in w["tabs"]):
                    return w
            return None

        # ── Seed defaults (same as the daemon) ─────────────────────────────────
        cdp_eval(sw_ws, "(async (s) => { await chrome.storage.local.set(s); return true; })(" +
                 json.dumps({"open_window_normal": True, "open_window_focused": True, "restore_focus_on_rental": False}) + ")")

        # ── Test A: main rent must NOT close the user's real site window ──────
        print("\n[Test A] main rent — user's real site window must SURVIVE")
        user_win = create_window("https://example.org/", incognito=False)
        blank_win = create_window("about:blank", incognito=False)      # extension-owned (startup tab)
        panel_win = create_window(f"chrome-extension://{ext_id}/panel/index.html", incognito=False)  # extension-owned
        time.sleep(0.8)
        before = window_urls()
        n_before = len(before)
        print(f"  prepared windows: user={user_win} blank={blank_win} panel={panel_win} (total {n_before})")

        rent_win = create_via_sw_port("https://rent.example/", "main")
        time.sleep(1.5)   # let cleanup run
        after = window_urls()
        rent_present = find_win(after, lambda u: u.startswith("https://rent.example/"))
        user_alive = find_win(after, lambda u: u.startswith("https://example.org/"))
        # user window id must be unchanged (not re-created)
        user_same_id = any(w["id"] == user_win and any(u.startswith("https://example.org/") for u in w["tabs"]) for w in after)

        ok_rent = rent_present is not None
        ok_user = user_same_id
        print(f"  rental window present: {'PASS' if ok_rent else 'FAIL'} (id={rent_present and rent_present['id']})")
        print(f"  user site window alive+same id: {'PASS' if ok_user else 'FAIL'} "
              f"(win {user_win} -> {'present' if user_alive else 'CLOSED'})")
        if not ok_user:
            print("    >> BUG REPRODUCED: main rent closed a user window (0.6.340 bug)")
        results["A: main rent keeps user's real site window"] = ok_user and ok_rent

        # ── Test B: main rent DOES close extension-owned windows ──────────────
        print("\n[Test B] main rent — extension-owned windows (blank/panel) are closed")
        blank_alive = any(w["id"] == blank_win for w in after)
        panel_alive = any(w["id"] == panel_win for w in after)
        ok_b = (not blank_alive) and (not panel_alive)
        print(f"  blank window ({blank_win}) alive: {'FAIL' if blank_alive else 'PASS (closed)'}")
        print(f"  panel window ({panel_win}) alive: {'FAIL' if panel_alive else 'PASS (closed)'}")
        results["B: main rent closes extension-owned windows"] = ok_b

        # cleanup test windows
        for wid in (blank_win, panel_win):
            cdp_eval(sw_ws, f"(async () => {{ try {{ await chrome.windows.remove({wid}); }} catch(e){{}} }})()")

        # ── Test C: incognito rent must NOT touch any window ──────────────────
        print("\n[Test C] incognito rent — none of the existing windows are closed")
        user2 = create_window("https://example.org/", incognito=False)
        # extension-owned startup tab for the incognito case too
        blank2 = create_window("about:blank", incognito=False)
        time.sleep(0.5)
        before_inc = window_urls()
        ids_before = {w["id"] for w in before_inc}
        rent_incog = create_via_sw_port("https://rent.example/", "incognito")
        time.sleep(1.5)
        after_inc = window_urls()
        ids_after = {w["id"] for w in after_inc}
        # pre-existing windows must ALL survive (ids present in the after set);
        # the incognito rental window itself may be an extra window.
        survived = ids_before.issubset(ids_after)
        user2_alive = any(w["id"] == user2 for w in after_inc)
        blank2_alive = any(w["id"] == blank2 for w in after_inc)
        print(f"  user2 window alive: {'PASS' if user2_alive else 'FAIL'}")
        print(f"  blank2 window alive: {'PASS' if blank2_alive else 'FAIL'}")
        print(f"  all pre-rent windows survived: {'PASS' if survived else 'FAIL'}")
        results["C: incognito rent leaves all existing windows"] = user2_alive and blank2_alive and survived

        # cleanup
        try:
            cdp_eval(sw_ws, f"(async () => {{ try {{ await chrome.windows.remove({rent_win}); }} catch(e){{}} }})()")
        except Exception:
            pass
        try:
            cdp_eval(sw_ws, f"(async () => {{ try {{ await chrome.windows.remove({rent_incog}); }} catch(e){{}} }})()")
        except Exception:
            pass
        try:
            cdp_eval(sw_ws, f"(async () => {{ try {{ await chrome.windows.remove({user_win}); }} catch(e){{}} }})()")
        except Exception:
            pass
        try:
            cdp_eval(sw_ws, f"(async () => {{ try {{ await chrome.windows.remove({user2}); }} catch(e){{}} }})()")
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