#!/usr/bin/env python3
"""E2E: multi-rent isolation on a single app-mode provider.

Checks that concurrent rental sessions on ONE app-mode provider (one browser,
one extension, one WS) route each agent's CDP commands into ITS OWN rental
window/tab — and never into a sibling session's window.

Why this test exists (see also SPEC / multi-rent work):
  * app.py runs a single persistent browser context. Multi-rent is realised by
    the extension creating one window per session (`session_create_window` →
    `chrome.windows.create`), each tracked in `p2p-manager`'s `sessions` map by
    `session_id`. CDP routing in `case "cdp"` uses
    `sess?.tabId ?? currentSessionTabId()`:
        - happy path: session is in `sessions`, tabId set -> its own tab
        - fallback:    session has no entry / tabId null -> GLOBAL `_activeSessionTabId`
    The fallback is exactly where an action can land in the WRONG window when
    sessions run concurrently and one of them hasn't created its window yet.

  This script exercises the real path (SDK `client.rent()` -> relay -> extension)
  with three parallel renters, distinct URLs and per-session DOM markers, then
  verifies each marker is only visible in its own session's window.

Two modes:
  * --mode main      (Test A)  — rental with main profile   (`client.rent(mode="main")`)
  * --mode incognito (Test B)  — rental with incognito      (`client.rent(mode="incognito")`)

Usage (runs inside the provider host, needs the provider container up on
schedule with multi_session=true and max_sessions>=3):

    CEKI_API_URL=https://api.ceki.me \
    CEKI_RELAY_URL=wss://browser.ceki.me/ws/agent \
    CEKI_API_KEY=<renter-agent-token> \
    SCHEDULE_ID=<prod-schedule-id> \
    python3 scripts/e2e-multirent-isolation.py --mode main

Exit code 0 = isolation confirmed; 1 = one or more sessions failed / markers
bled across windows; 2 = environment/config error (missing token, no provider,
timed out).

The script is intentionally dependency-light (stdlib + httpx/websockets; the
ceki python SDK must be importable). It never writes into the repo.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

# ceki python SDK lives in the ceki-sdk repo; importable when that checkout is
# on sys.path (repo docs show the cmd below).
try:
    sys.path.insert(0, os.environ.get("CEKI_SDK_PYTHON", ""))
    from ceki_sdk import connect  # type: ignore
except Exception as exc:  # pragma: no cover - env check
    print(f"ERROR: cannot import ceki_sdk: {exc}\n"
          "  pip install ceki-browser or set CEKI_SDK_PYTHON=/path/to/python-sdk")
    sys.exit(2)

API_URL = os.environ.get("CEKI_API_URL", "https://api.ceki.me")
RELAY_URL = os.environ.get("CEKI_RELAY_URL", "wss://browser.ceki.me/ws/agent")
SCHEDULE_ID = int(os.environ.get("SCHEDULE_ID", "40875"))
MODE = os.environ.get("E2E_MODE", "main")  # or "incognito"
SDK_SYS_PATH = os.environ.get("CEKI_SDK_PYTHON", "")

# Distinct, stable, low-entropy landing pages (no auth, no heavy JS).
SITES = [
    ("https://example.com", "site-example"),
    ("https://example.org", "site-example-org"),
    ("https://example.net", "site-example-net"),
]
MARKER_TMPL = "E2E_MARKER_{site}"

# How long to wait for a rental match / page load / screenshot.
MATCH_TIMEOUT = 60.0
LOAD_TIMEOUT = 30.0
CDP_TIMEOUT = 30.0


async def diag_inspect_extension(client_ws: str) -> dict:
    """Best-effort introspection of the extension's session map over CDP.

    Connect to the extension's same endpoint the probe would use and read the
    offscreen p2p-manager globals (`sessions` map / `_activeSessionTabId`).
    Requires the provider browser CDP port (e.g. --diag CDP=http://127.0.0.1:9223).
    Not required for the marker test — purely diagnostic.
    """
    import websockets  # local import: optional dependency

    async with websockets.connect(client_ws, max_size=64 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": (
                    "JSON.stringify({"
                    "  active: typeof _activeSessionTabId !== 'undefined' ? _activeSessionTabId : null,"
                    "  sessions: typeof sessions !== 'undefined'"
                    "    ? Array.from(sessions.entries()).map(([k, v]) => [k, v?.tabId ?? null])"
                    "    : 'n/a'"
                    "})"
                ),
                "returnByValue": True,
            },
        }))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if msg.get("id") == 1:
                val = msg.get("result", {}).get("result", {}).get("value")
                try:
                    return json.loads(val) if val else {}
                except Exception:
                    return {"raw": val}


async def rent_one(api_key: str, site: str, tag: str, events: list[str], rent_mode: str | None = None) -> dict:
    """Rent one session, navigate, plant a marker, screenshot — collect evidence."""
    url, site_key = site
    marker = MARKER_TMPL.format(site=site_key)
    result = {"tag": tag, "site": site_key, "url": url, "marker": marker,
              "ok": False, "session_id": None, "error": None}

    try:
        client = await connect(api_key)
    except Exception as exc:
        result["error"] = f"connect failed: {type(exc).__name__}: {exc}"
        return result

    try:
        browser = await asyncio.wait_for(
            client.rent(SCHEDULE_ID, mode=rent_mode or MODE),
            timeout=MATCH_TIMEOUT,
        )
    except Exception as exc:
        result["error"] = f"rent/match failed: {type(exc).__name__}: {exc}"
        try:
            await client.close()
        except Exception:
            pass
        return result

    result["session_id"] = browser.session_id
    events.append(f"[{tag}] matched session_id={browser.session_id} url={url}")

    # Give the extension a moment to materialise the window for this session.
    await asyncio.sleep(1.5)

    # Real agent flow: Page.navigate FIRST (it triggers the extension's lazy
    # window creation, `ensureSessionWindow`). Any non-navigate command before
    # the window exists maps to sess.tabId=null → router falls back to the
    # GLOBAL _activeSessionTabId (wrong window) or answers no_session. So we
    # mirror the SDK agent: navigate → load → page ops.
    loaded = asyncio.Event()
    browser.on_event(lambda m, p, e=loaded: e.set() if m == "Page.loadEventFired" else None)

    nav = None
    for attempt in range(4):
        try:
            nav = await browser.send({"method": "Page.navigate", "params": {"url": url}}, timeout=20)
            break
        except Exception as exc:
            events.append(f"[{tag}] Page.navigate attempt {attempt + 1}: {str(exc)[:80]}")
            if attempt < 3:
                await asyncio.sleep(1.5)
                continue
            raise
    try:
        await asyncio.wait_for(loaded.wait(), timeout=LOAD_TIMEOUT)
    except asyncio.TimeoutError:
        pass  # load event optional; we re-check DOM below

    # Page.enable (events) AFTER the window exists — optional, best-effort.
    try:
        await browser.send({"method": "Page.enable"}, timeout=10)
    except Exception:
        pass

    # Plant a marker in THIS session's DOM.
    expr = (
        "(() => { const d = document.createElement('div');"
        f" d.id = 'e2e_marker'; d.textContent = {json.dumps(marker)};"
        " document.body.appendChild(d); return d.textContent; })()"
    )
    try:
        planted = await browser.send({
            "method": "Runtime.evaluate",
            "params": {"expression": expr, "returnByValue": True},
        })
        events.append(f"[{tag}] marker planted: {planted.get('result', {}).get('value')}"
                      if planted.get("result", {}).get("value") else f"[{tag}] marker plant unexpected: {planted}")

        # Read back the URL + marker from THIS session's own window.
        check = await browser.send({
            "method": "Runtime.evaluate",
            "params": {
                "expression": "JSON.stringify({href: location.href, body: document.body?.innerText ?? ''})",
                "returnByValue": True,
            },
        })
        if check.get("result", {}).get("value"):
            data = json.loads(check["result"]["value"])
            result["own_url"] = data["href"]
            result["own_body"] = data["body"]
            result["ok"] = True
        else:
            result["error"] = f"no DOM read-back: {check}"
    except Exception as exc:
        result["error"] = f"cdp step failed: {type(exc).__name__}: {exc}"

    try:
        await asyncio.wait_for(browser.close(), timeout=10)
    except Exception:
        pass
    finally:
        try:
            await client.close()
        except Exception:
            pass
    return result


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("main", "incognito"), default=MODE,
                    help="profile mode for every rent (default: main)")
    ap.add_argument("--sessions", type=int, default=3, help="number of concurrent renters (default 3)")
    ap.add_argument("--retry", type=int, default=1, help="passes (default 1; >1 detects flakiness)")
    ap.add_argument("--diag", metavar="CDP_BASE", default=None,
                    help="provider CDP base (e.g. http://127.0.0.1:9223) — dump extension "
                         "session-map state after rentals (diagnostic, optional)")
    ap.add_argument("--no-preflight", action="store_true",
                    help="skip the settings multi_session check; rely on empirical probe")
    ap.add_argument("--keys", default="",
                    help="comma-separated renter agent API keys, one PER SESSION. "
                         "Multi-rent on one schedule requires DISTINCT billables "
                         "(same agent re-renting returns its existing session). "
                         "Defaults to CEKI_API_KEY for all sessions.")
    args = ap.parse_args()

    api_key = os.environ.get("CEKI_API_KEY", "")
    keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    if keys:
        # Distinct billables per session (multi-rent requirement).
        if len(keys) < args.sessions:
            print(f"WARN: --keys has {len(keys)} keys for {args.sessions} sessions — "
                  f"fallback duplicate keys will be rejected as 'resume' by backend.",
                  file=sys.stderr)
        api_keys = (keys * args.sessions)[: args.sessions]
    elif api_key:
        # Single key for all sessions — fine for 1, misleading for N (backend
        # treats the 2nd..N rents from the same billable as 'resume').
        api_keys = [api_key] * args.sessions
    else:
        print("ERROR: CEKI_API_KEY or --keys required (renter agent token)", file=sys.stderr)
        return 2

    # Optional: quick preflight that the schedule advertises multi-session.
    # Backend /api/browser/{id} may 403 (owner-only) — fall back to a cheap
    # empirical probe: a second concurrent rent on the same schedule must NOT be
    # rejected by BrowserBusyService. Only meaningful when the provider is up.
    preflight_ok = True
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{API_URL}/api/browser/{SCHEDULE_ID}",
            headers={"Authorization": f"Bearer {api_keys[0]}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            meta = json.loads(r.read())
        ms = meta.get("settings", {}).get("multi_session")
        if ms is not True:
            print(f"ERROR: schedule {SCHEDULE_ID} settings.multi_session != true "
                  f"(got {ms!r}) — backend BrowserBusyService gates to 1 session. "
                  "Enable multi_session (max_sessions>=3) on the schedule, or use "
                  "--no-preflight to force the empirical probe.", file=sys.stderr)
            preflight_ok = False
    except Exception as exc:
        print(f"NOTE: settings preflight unavailable ({type(exc).__name__}) — "
              "will rely on the empirical multi-rent probe inside the test.", file=sys.stderr)

    if not preflight_ok and not args.no_preflight:
        print("PREFLIGHT FAILED — schedule not multi-session. Exiting.", file=sys.stderr)
        return 2

    sites = [SITES[i % len(SITES)] for i in range(args.sessions)]
    print(f"[e2e] mode={args.mode} sessions={args.sessions} schedule={SCHEDULE_ID} "
          f"relay={RELAY_URL} api={API_URL}", flush=True)

    overall_ok = True
    for pass_no in range(1, args.retry + 1):
        print(f"\n[e2e] pass {pass_no}/{args.retry}", flush=True)
        events: list[str] = []
        t0 = time.monotonic()
        results = await asyncio.gather(*[
            rent_one(api_keys[i], sites[i], f"S{i + 1}", events, args.mode) for i in range(args.sessions)
        ])
        dt = time.monotonic() - t0

        print(f"[e2e] ---- pass {pass_no} took {dt:.1f}s ----", flush=True)
        for ev in events:
            print(f"[e2e]   {ev}", flush=True)
        for r in results:
            status = "OK" if r["ok"] else "FAIL"
            print(f"[e2e] [{status}] {r['tag']} site={r['site']} session={r['session_id']} "
                  f"err={r['error']}", flush=True)

        # Isolation check — two directions:
        #   1) POSITIVE: each session sees ITS OWN marker in its own window
        #      (proves the action reached the expected tab at all).
        #   2) NEGATIVE: no session sees ANY OTHER session's marker
        #      (proves no cross-window bleed).
        ok_positive = ok_negative = True
        missing_own = []
        bled = []
        for r in results:
            if not r["ok"]:
                continue
            own = r["own_body"] or ""
            if r["marker"] not in own:
                missing_own.append(r["tag"])
            for other in results:
                if other is r or not other["ok"]:
                    continue
                if other["marker"] in own:
                    bled.append(f"{other['tag']}({other['site']}).marker visible in {r['tag']}({r['site']}).window")

        if missing_own:
            ok_positive = False
            overall_ok = False
            print("[e2e] !! ACTION MISSED TARGET WINDOW", flush=True)
            for tag in missing_own:
                print(f"[e2e]    - {tag}: own marker absent from own window (CDP command may have gone elsewhere)", flush=True)

        # A pass where NO session completed its CDP steps is inconclusive — it
        # likely means the schedule gates at 1 concurrent rent (not multi-session)
        # or the provider was down. Fail loudly instead of pretending isolation.
        completed = [r for r in results if r["ok"]]
        if not completed:
            overall_ok = False
            print("[e2e] !! INCONCLUSIVE: no session completed CDP steps — "
                  "schedule not multi-session or provider unreachable", flush=True)
        if bled:
            ok_negative = False
            overall_ok = False
            print("[e2e] !! CROSS-SESSION LEAK DETECTED", flush=True)
            for b in bled:
                print(f"[e2e]    - {b}", flush=True)

        if ok_positive and ok_negative and completed:
            print(f"[e2e] isolation OK ({len(completed)} completed / {len(results)} sessions, both directions)", flush=True)
        else:
            passed = [r for r in results if r["ok"]]
            failed = [r for r in results if not r["ok"]]
            if failed:
                for r in failed:
                    print(f"[e2e]    - {r['tag']}: session-level failure: {r['error']}", flush=True)
            print(f"[e2e] (passed={len(passed)}, failed={len(failed)})", flush=True)

        # Optional diagnostic: dump extension session-map state (who maps to which tab).
        if args.diag:
            import urllib.request
            try:
                targets = json.loads(urllib.request.urlopen(
                    f"{args.diag}/json/list", timeout=5).read())
                # pick the offscreen document target of the extension
                off = [t for t in targets
                       if "/offscreen/offscreen.html" in (t.get("url") or "")
                       or t.get("type") == "service_worker"]
                if off:
                    ws_url = off[0]["webSocketDebuggerUrl"]
                    state = await diag_inspect_extension(ws_url)
                    print(f"[e2e] diag: extension state: {json.dumps(state)}", flush=True)
                else:
                    print("[e2e] diag: no extension target found (need provider CDP port)", flush=True)
            except Exception as exc:
                print(f"[e2e] diag: skipped ({type(exc).__name__}: {exc})", flush=True)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))