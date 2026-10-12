#!/usr/bin/env python3
"""
e2e-multirent-app.py — multi-rent in APP mode (parallel concurrent rents).

Black-box acceptance inspired by the daemon-parallel suite (ev 9095) but for
the APP-mode path: several agents rent the SAME schedule CONCURRENTLY through
the real relay, each driving its own session. This is the scenario the
presence-WS connect fix (config-resolved guard in offscreen/ws.ts) must not
break: one offscreen document serving N concurrent rents.

Unlike e2e-daemon-parallel.sh (which expects the backend to gate to one
admitted session), this test works against a schedule that ALLOWS N concurrent
sessions (the dev daemon schedules are max_sessions=10). Pass a count and it
fires N rents at once.

Usage (live infra):
    RENTER_AGENT_TOKEN=<token> python3 scripts/e2e-multirent-app.py [schedule] [count]

Env overrides (dev host by default — the agent session env points at prod,
which does not host these schedules):
    CEKI_RELAY_URL_DEV, CEKI_API_URL_DEV
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python-sdk"))

from ceki_sdk import ConnectOptions, connect

RENTER = os.environ.get("RENTER_AGENT_TOKEN", "")
RENTER_TOKENS = [t for t in os.environ.get("RENTER_TOKENS", "").split() if t]
RELAY = os.environ.get("CEKI_RELAY_URL_DEV", "wss://browser.ittribe.org/ws/agent")
API = os.environ.get("CEKI_API_URL_DEV", "https://clawapi.ittribe.org")


async def one_rent(sid: int, idx: int, renter: str) -> dict:
    """Rent one browser with a DEDICATED renter token (parallel app-mode rents
    must come from distinct agents — relay resumes/waits same-renter sessions
    instead of opening a fresh one)."""
    t0 = time.monotonic()
    out = {"idx": idx, "sid": None, "renter": renter[:6] + "…", "rent_s": None, "nav": None, "shot_bytes": None}
    c = None
    b = None
    try:
        c = await asyncio.wait_for(
            connect(renter, ConnectOptions(relay_url=RELAY, api_url=API, reconnect=False)), 20
        )
        b = await asyncio.wait_for(c.rent(sid), timeout=90)
        out["sid"] = b.session_id
        out["rent_s"] = round(time.monotonic() - t0, 1)
        await asyncio.wait_for(b.navigate("https://example.com", timeout=30000), 40)
        out["nav"] = "ok"
        try:
            shot = await asyncio.wait_for(b.screenshot(timeout=45000), timeout=52)
        except (asyncio.TimeoutError, Exception):
            # 3 concurrent captures on one browser can momentarily stall a
            # render; retry once before declaring failure.
            shot = await asyncio.wait_for(b.screenshot(timeout=45000), timeout=52)
        if isinstance(shot, dict):
            data = shot.get("data") or ""
            out["shot_bytes"] = len(data) if isinstance(data, str) else -1
        elif isinstance(shot, bytes):
            out["shot_bytes"] = len(shot)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        if b:
            try:
                await b.close()
            except Exception:
                pass
        if c:
            try:
                await c.disconnect()
            except Exception:
                pass
        out["total_s"] = round(time.monotonic() - t0, 1)
    return out


async def main() -> None:
    if not RENTER and not RENTER_TOKENS:
        print("FATAL: RENTER_AGENT_TOKEN or RENTER_TOKENS unset (live infra only)")
        sys.exit(2)
    sid = int(sys.argv[1]) if len(sys.argv) > 1 else 42306
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    if RENTER_TOKENS:
        # Multi-renter mode: use a distinct token per concurrent rent.
        if len(RENTER_TOKENS) < n:
            print(f"FATAL: RENTER_TOKENS has {len(RENTER_TOKENS)} tokens, need {n}")
            sys.exit(2)
        renters = RENTER_TOKENS[:n]
    else:
        renters = [RENTER] * n
    print(f"=== e2e-multirent APP-mode: {n} concurrent rents, {len(set(renters))} distinct renter(s), schedule {sid} (LIVE) ===", flush=True)

    start = time.monotonic()
    results = await asyncio.gather(*[one_rent(sid, i, renters[i]) for i in range(n)])
    wall = time.monotonic() - start

    passed = 0
    for r in sorted(results, key=lambda x: (x.get("error") is not None, x["idx"])):
        ok = (
            r.get("nav") == "ok"
            and r.get("shot_bytes") is not None
            and int(r.get("shot_bytes") or 0) > 1000
        )
        passed += 1 if ok else 0
        print(
            f"  {'PASS' if ok else 'FAIL'} #{r['idx']} renter={r.get('renter')} sid={r.get('sid')} "
            f"rent={r.get('rent_s')}s nav={r.get('nav')} shot={r.get('shot_bytes')}B "
            f"total={r.get('total_s')}s"
            + (f" err={r.get('error')}" if "error" in r else ""),
            flush=True,
        )
    print(f"=== result: {'PASS' if passed == n else 'FAIL'} ({passed}/{n} concurrent rents ok) wall={wall:.1f}s ===")
    sys.exit(0 if passed == n else 1)


if __name__ == "__main__":
    asyncio.run(main())
