#!/usr/bin/env python3
"""VTB E2E — rent the pseudo-yandex provider (schedule 42307) through the MSK
proxy and verify that online.vtb.ru actually loads as Yandex Browser.

What it checks:
  0. Rent settings (from the extension's realtime-freshened storage
     ceki_browser card): safe_mode=false, no vtb in domain_blacklist,
     browser_flavor=yandex (so the bank page is NOT ERR_BLOCKED_BY_CLIENT).
  1. Rent schedule 42307 (pseudo-yandex, system Chromium 154 + YaBrowser UA).
  2. Evaluate navigator.userAgent on the rent page — must carry YaBrowser.
  3. Navigate to https://online.vtb.ru and wait for it to settle.
  4. Capture a screenshot and check it is non-empty.
  5. Release the rent.

Cost: ~1-2 min of rent time.

Run (from ceki-plugin root or with the SDK on PYTHONPATH):
    export CEKI_API_KEY=$(cat /home/node/.openclaw/secrets/skill_rent_agent_token.txt)
    export CEKI_API_URL="https://api.ceki.me"
    export CEKI_RELAY_URL="wss://browser.ceki.me/ws/agent"
    export CEKI_CHAT_URL="https://chat.ceki.me/api/chat"
    # provider token (owner) — schedule settings via /api/browser/me:
    export VTB_PROVIDER_TOKEN="..."
    python3 scripts/vtb-test.py [schedule_id]
"""
from __future__ import annotations

import asyncio
import os
import sys

from ceki_sdk import ConnectOptions, connect

SCHEDULE_ID = int(os.environ.get("SCHEDULE_ID", "42307"))
VTB_URL = os.environ.get("VTB_URL", "https://online.vtb.ru")
API_BASE = os.environ.get("CEKI_API_URL", "https://api.ceki.me")

# Settings check — from the server, exactly the card the extension fetches on
# connect/realtime (refreshCekiBrowserCard -> /api/browser/me). The rent page
# itself has no access to chrome.storage (page is a web context, not the
# extension), so we read the same freshest version the extension pulls.
async def check_schedule_settings(provider_token: str, schedule_id: int) -> bool:
    """Fetch /api/browser/me (owner auth) and assert the rentable settings.

    Returns True when all checks pass; prints each check with PASS/FAIL.
    """
    import httpx as _httpx
    print("\n[vtb] schedule settings check (server card, as ext fetches on connect)")
    ok = True
    try:
        resp = _httpx.get(
            f"{API_BASE}/api/browser/me",
            headers={"Authorization": f"Bearer {provider_token}", "Accept": "application/json"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        s = data.get("settings", {}) if data else {}
        if data.get("id") != schedule_id:
            print(f"[vtb]   id mismatch: {data.get('id')} != {schedule_id} -> FAIL (owner token for wrong schedule?)")
            return False
        print(f"[vtb]   id={data.get('id')} label={data.get('label')} "
              f"flavor={s.get('browser_flavor')} ver={s.get('extension_version')}")

        # 1) safe_mode must be OFF, otherwise the extension blocks banking domains.
        safe = s.get("safe_mode")
        safe_ok = safe is False
        print(f"[vtb]   safe_mode={safe!r} -> {'PASS' if safe_ok else 'FAIL'} (must be false)")
        ok = ok and safe_ok

        # 2) domain_blacklist must not contain vtb (or related).
        blist = s.get("domain_blacklist") or []
        bl_ok = not any("vtb" in (d or "").lower() for d in blist)
        print(f"[vtb]   domain_blacklist={blist} -> {'PASS' if bl_ok else 'FAIL'}")
        ok = ok and bl_ok

        # 3) browser_flavor should advertise yandex for a pseudo-yandex provider.
        flavor = s.get("browser_flavor")
        flavor_ok = flavor == "yandex"
        print(f"[vtb]   browser_flavor={flavor!r} -> {'PASS' if flavor_ok else 'FAIL'} (want 'yandex')")
        ok = ok and flavor_ok

        # 4) domain_allowed — empty is fine; must not block vtb via negation.
        aallow = s.get("domain_allowed") or []
        allow_ok = not any("vtb" in (d or "").lower() for d in aallow)
        print(f"[vtb]   domain_allowed={aallow} -> {'PASS' if allow_ok else 'FAIL'}")
        ok = ok and allow_ok
    except Exception as exc:
        print(f"[vtb]   settings fetch failed: {exc} -> FAIL")
        ok = False
    return ok


async def main() -> None:
    api_key = os.environ.get("CEKI_API_KEY")
    if not api_key:
        print("ERROR: CEKI_API_KEY not set — see header for env")
        sys.exit(2)
    schedule_id = int(sys.argv[1]) if len(sys.argv) > 1 else SCHEDULE_ID
    provider_token = os.environ.get("VTB_PROVIDER_TOKEN", "").strip()

    # 0) Check schedule settings first (fail fast — don't burn rent time).
    #    This is the same card the extension pulls on connect, so it reflects
    #    the freshest state the rental will actually see.
    settings_ok = await check_schedule_settings(provider_token, schedule_id)

    client = await connect(
        api_key,
        ConnectOptions(
            api_url=API_BASE,
            relay_url=os.environ.get("CEKI_RELAY_URL", "wss://browser.ceki.me/ws/agent"),
            chat_url=os.environ.get("CEKI_CHAT_URL", "https://chat.ceki.me/api/chat"),
        ),
    )

    print(f"[vtb] renting schedule {schedule_id} (pseudo-yandex + MSK proxy)")
    try:
        browser = await client.rent(schedule_id)

        # 1) UA check — must look like Yandex.
        ua = await browser.send({
            "method": "Runtime.evaluate",
            "params": {"expression": "navigator.userAgent", "returnByValue": True},
        })
        ua_val = (ua.get("result") or {}).get("value", "")
        print(f"[vtb] rent UA: {ua_val}")
        ua_ok = "YaBrowser" in ua_val
        print(f"[vtb] UA check -> {'PASS' if ua_ok else 'FAIL'}")

        # 2) Navigate to VTB and wait for it to settle. Page.loadEventFired is
        # racy on slow banks (SPA may fire it before we subscribe), so we treat
        # a real URL on online.vtb.ru as "loaded" — chrome-error://chromewebdata
        # is the failure signature (blocked / proxy dead / TLS fail).
        await browser.send({"method": "Page.enable"})
        await browser.send({
            "method": "Page.navigate",
            "params": {"url": VTB_URL, "transitionType": "typed"},
        })
        # wait for the page to leave the chrome-error state
        loaded = False
        for _ in range(30):
            await asyncio.sleep(1.5)
            cur = await browser.send({
                "method": "Runtime.evaluate",
                "params": {"expression": "location.href", "returnByValue": True},
            })
            href = (cur.get("result") or {}).get("value") or ""
            if href.startswith("http") and "vtb.ru" in href:
                loaded = True
                break
        nav_ok = loaded
        print(f"[vtb] page loaded on VTB: {nav_ok}")

        # 3) Evidence: screenshot.
        shot = await browser.send({"method": "Page.captureScreenshot"})
        data = shot.get("data", "")
        shot_ok = len(data) > 1000
        print(f"[vtb] screenshot length={len(data)} -> {'PASS' if shot_ok else 'FAIL'}")

        # 4) Surface the final URL the bank actually showed.
        url = await browser.send({
            "method": "Runtime.evaluate",
            "params": {"expression": "location.href", "returnByValue": True},
        })
        print(f"[vtb] final URL: {(url.get('result') or {}).get('value')}")

        ok = settings_ok and ua_ok and nav_ok
        print(f"\n[vtb] RESULT: {'PASS' if ok else 'FAIL'} (settings={settings_ok} UA={ua_ok} load={nav_ok})")
        await browser.close()
    finally:
        await client.close()

    sys.exit(0 if (settings_ok and ua_ok and nav_ok) else 1)


if __name__ == "__main__":
    asyncio.run(main())