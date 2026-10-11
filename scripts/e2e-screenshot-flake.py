#!/usr/bin/env python3
"""
P1 e2e test — screenshot() returns 0 bytes on live provider (flake: 0/1/145KB).

Observed on live infra (chromium provider, real relay/backend/Chrome):
- b.send({Page.captureScreenshot, params:{format:"png",optimizeForSpeed:true}})
  → 145KB PNG data (flat "data" key).
- b.screenshot() (SDK method, params:{optimizeForSpeed:true}, no "format")
  → 0/1 bytes when the response comes back over the P2P data channel,
    even though the same rent's navigate works.

This test reproduces against a LIVE provider and prints, per rent:
  method A (raw send, format=png) bytes
  method B (SDK screenshot) bytes
  raw DC response payload shape from the extension (captured by hooking the
  CDP future resolution) so we can see what the extension actually returns.

Run (live infra):
  RENTER_AGENT_TOKEN=<token> python3 scripts/e2e-screenshot-flake.py 42306 3
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python-sdk"))

from ceki_sdk import ConnectOptions, connect

RENTER = os.environ.get("RENTER_AGENT_TOKEN", "")
# The agent session env (settings.json) exports CEKI_RELAY_URL/CEKI_API_URL
# pointing at the PROD host (browser.ceki.me) which does NOT host these dev
# schedules — renting through it yields instant `no_providers`. Use the dev
# endpoints by DEFAULT (matching where 42306/42307 live); override only via
# explicit *_DEV_* vars so local runs never silently hit the wrong relay.
RELAY = os.environ.get("CEKI_RELAY_URL_DEV", "wss://browser.ittribe.org/ws/agent")
API = os.environ.get("CEKI_API_URL_DEV", "https://clawapi.ittribe.org")


def trunc(s, n=300):
    s = str(s)
    return s if len(s) <= n else s[:n] + f"…({len(s)})"


async def probe_once(sid: int, idx: int) -> dict:
    c = await asyncio.wait_for(
        connect(RENTER, ConnectOptions(relay_url=RELAY, api_url=API, reconnect=False)), 20
    )
    b = None
    out = {"idx": idx, "sid": None, "nav": None, "A_bytes": None, "B_bytes": None, "raw_dc": None}
    try:
        b = await asyncio.wait_for(c.rent(sid), timeout=90)
        out["sid"] = b.session_id
        await asyncio.wait_for(b.navigate("https://example.com", timeout=30000), 40)
        out["nav"] = "ok"

        # Method A: raw send with format=png (the path that previously worked).
        try:
            resp = await asyncio.wait_for(
                b.send(
                    {"method": "Page.captureScreenshot", "params": {"format": "png", "optimizeForSpeed": True}},
                    timeout=30000,
                ),
                timeout=35,
            )
            # Capture what the SDK saw (raw response shape).
            out["raw_dc"] = {k: (len(v) if isinstance(v, str) else type(v).__name__) for k, v in resp.items()}
            data = resp.get("result", {}).get("data") or resp.get("data") or ""
            out["A_bytes"] = len(data) if isinstance(data, str) else -1
        except Exception as e:
            out["A_error"] = f"{type(e).__name__}: {str(e)[:80]}"

        # Method B: SDK screenshot() default (base64).
        try:
            shot = await asyncio.wait_for(b.screenshot(timeout=30000), timeout=35)
            if isinstance(shot, dict):
                data = shot.get("data") or ""
                out["B_bytes"] = len(data) if isinstance(data, str) else -1
            elif isinstance(shot, bytes):
                out["B_bytes"] = len(shot)
            else:
                out["B_bytes"] = f"type={type(shot).__name__}"
        except Exception as e:
            out["B_error"] = f"{type(e).__name__}: {str(e)[:80]}"

    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        if b:
            try:
                await b.close()
            except Exception:
                pass
        try:
            await c.disconnect()
        except Exception:
            pass
    return out


async def main() -> None:
    if not RENTER:
        print("FATAL: RENTER_AGENT_TOKEN unset (live infra only)")
        sys.exit(2)
    sid = int(sys.argv[1]) if len(sys.argv) > 1 else 42306
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    print(f"=== P1 e2e screenshot-flake on schedule {sid}, {n} rents (LIVE) ===", flush=True)
    fails = 0
    for i in range(n):
        r = await probe_once(sid, i)
        # verdict: A must be >1000 bytes; B must be >1000 bytes (non-empty PNG/base64)
        ok_a = isinstance(r.get("A_bytes"), int) and r["A_bytes"] > 1000
        ok_b = isinstance(r.get("B_bytes"), int) and r["B_bytes"] > 1000
        verdict = "PASS" if (ok_a and ok_b) else "FAIL"
        if verdict == "FAIL":
            fails += 1
        print(
            f"  {verdict} #{i} sid={r.get('sid')} nav={r.get('nav')} "
            f"A(raw/format)={r.get('A_bytes')} B(sdk/base64)={r.get('B_bytes')} "
            f"raw_dc={trunc(r.get('raw_dc'))}"
            + (f" Aerr={r.get('A_error')}" if "A_error" in r else "")
            + (f" Berr={r.get('B_error')}" if "B_error" in r else "")
            + (f" err={r.get('error')}" if "error" in r else ""),
            flush=True,
        )
        await asyncio.sleep(6)
    print(f"=== P1 result: {'FAIL' if fails else 'PASS'} ({n - fails}/{n} passes) ===")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main())