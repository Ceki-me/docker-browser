#!/usr/bin/env python3
"""
E2E: sequential rents — same renter, rent -> close -> rent -> close.

Covers the "persist / sequential" acceptance gap (e2e-daemon-parallel.sh b
was only a skeleton). Proves that a renter can chain back-to-back rentals of
the SAME schedule with distinct sessions: each cycle must mint its own
session_id (not resume into the previous one), rent, navigate, screenshot,
close cleanly, and the next rent must come up fresh.

Run (live infra, dev contour):
  RENTER_AGENT_TOKEN=<agent token> \
  CEKI_RELAY_URL_DEV=wss://browser.ittribe.org/ws/agent \
  CEKI_API_URL_DEV=https://clawapi.ittribe.org \
  python3 scripts/e2e-sequential-rents.py [schedule] [cycles]
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python-sdk"))

from ceki_sdk import ConnectOptions, connect

RENTER = os.environ.get("RENTER_AGENT_TOKEN", "")
RELAY = os.environ.get("CEKI_RELAY_URL_DEV", "wss://browser.ittribe.org/ws/agent")
API = os.environ.get("CEKI_API_URL_DEV", "https://clawapi.ittribe.org")


async def one_cycle(c, sid: int, idx: int) -> dict:
    """Rent -> navigate -> screenshot -> close. Returns per-cycle metrics."""
    out = {"idx": idx, "sid": None, "rent_s": None, "nav": None, "shot_bytes": None, "error": None}
    b = None
    t0 = time.monotonic()
    try:
        b = await asyncio.wait_for(c.rent(sid), timeout=90)
        out["sid"] = b.session_id
        out["rent_s"] = round(time.monotonic() - t0, 1)
        await asyncio.wait_for(b.navigate("https://example.com", timeout=30000), 40)
        out["nav"] = "ok"
        try:
            shot = await asyncio.wait_for(b.screenshot(timeout=45000), timeout=52)
        except (asyncio.TimeoutError, Exception):
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
                await asyncio.wait_for(b.close(), timeout=30)
            except Exception:
                pass
        out["total_s"] = round(time.monotonic() - t0, 1)
    return out


async def main() -> None:
    if not RENTER:
        print("FATAL: RENTER_AGENT_TOKEN unset (live infra only)")
        sys.exit(2)
    sid = int(sys.argv[1]) if len(sys.argv) > 1 else 40693
    cycles = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    print(f"=== e2e sequential-rents: schedule {sid}, {cycles} cycles (LIVE) ===", flush=True)

    c = await asyncio.wait_for(
        connect(RENTER, ConnectOptions(relay_url=RELAY, api_url=API, reconnect=False)), 20
    )
    prev_sid = None
    passed = 0
    try:
        for i in range(cycles):
            r = await one_cycle(c, sid, i)
            distinct = prev_sid is None or r["sid"] != prev_sid
            ok = r["error"] is None and r["nav"] == "ok" and (r["shot_bytes"] or 0) > 0 and distinct
            if ok:
                passed += 1
            print(
                f"  {'PASS' if ok else 'FAIL'} #{r['idx']} sid={r.get('sid')} "
                f"rent={r.get('rent_s')}s nav={r.get('nav')} shot={r.get('shot_bytes')}B "
                f"total={r.get('total_s')}s distinct={distinct}"
            )
            if r["error"]:
                print(f"        err={r['error']}")
            prev_sid = r.get("sid")
    finally:
        try:
            await c.disconnect()
        except Exception:
            pass
    print(f"=== result: {'PASS' if passed == cycles else 'FAIL'} ({passed}/{cycles} sequential rents ok) ===")
    sys.exit(0 if passed == cycles else 1)


if __name__ == "__main__":
    asyncio.run(main())