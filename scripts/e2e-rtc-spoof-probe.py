#!/usr/bin/env python3
"""
E2E probe for ev 16854 — WebRTC local-IP spoof.

Rents a schedule, opens a data: URL page that creates an RTCPeerConnection
(no STUN/TURN — host candidates only) and reports every ICE candidate +
the final localDescription SDP. Asserts:
  * NO 172.16/12, NO 10.x in candidates/SDP (container infra masked)
  * at least one 192.168.x.x host candidate present (the fake range)

Run (live infra, dev contour):
  RENTER_AGENT_TOKEN=<renter token> bash -c '
    CEKI_RELAY_URL_DEV=wss://browser.ittribe.org/ws/agent \
    CEKI_API_URL_DEV=https://clawapi.ittribe.org \
    python3 scripts/e2e-rtc-spoof-probe.py <schedule>'
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python-sdk"))
from ceki_sdk import ConnectOptions, connect  # noqa: E402

RENTER = os.environ.get("RENTER_AGENT_TOKEN", "")
RELAY = os.environ.get("CEKI_RELAY_URL_DEV", "wss://browser.ittribe.org/ws/agent")
API = os.environ.get("CEKI_API_URL_DEV", "https://clawapi.ittribe.org")

PROBE_HTML = r"""<!DOCTYPE html><html><body><h1>rtc-probe</h1><pre id="out"></pre>
<script>
var out = [];
function log(s){ out.push(s); document.getElementById('out').textContent = out.join('\n'); }
async function run() {
  try {
    var pc = new RTCPeerConnection();
    pc.onicecandidate = function(ev) {
      if (ev.candidate) log('CAND ' + ev.candidate.candidate);
      else {
        log('DONE');
        try { log('LD ' + (pc.localDescription ? pc.localDescription.sdp.replace(/\n/g, ' | ') : 'null')); } catch(e) { log('LDERR ' + e); }
      }
    };
    pc.createDataChannel('probe');
    var off = await pc.createOffer();
    log('OFFER ' + off.sdp.replace(/\n/g, ' | '));
    await pc.setLocalDescription(off);
    setTimeout(function(){ if (!/DONE/.test(out.join('\n'))) log('TIMEOUT'); }, 8000);
  } catch(e) { log('ERR ' + e.message); }
}
run();
</script></body></html>
"""


def check_report(rpt: str, label: str) -> dict:
    import re
    ips = set(re.findall(r"\d+\.\d+\.\d+\.\d+", rpt))
    bad = [ip for ip in ips if (
        ip.startswith("10.") or
        (ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31)
    )]
    fake = [ip for ip in ips if ip.startswith("192.168.")]
    print(f"[{label}] IPs seen: {sorted(ips)}")
    print(f"[{label}] bad(172/10): {bad}  fake(192.168): {fake}")
    return {"bad": bad, "fake": fake}


async def main():
    sid = int(sys.argv[1]) if len(sys.argv) > 1 else 40693
    toks = [t for t in RENTER.split(",") if t]
    last_err = None
    for tok in toks:
        try:
            async with connect(tok, ConnectOptions(relay=RELAY, api_url=API)) as c:
                print(f"connecting with token {'****' + tok[-6:]}")
                t0 = time.monotonic()
                b = await asyncio.wait_for(c.rent(sid), timeout=100)
                print(f"rented sid={b.session_id} in {round(time.monotonic()-t0,1)}s")
                # wait a bit for fingerprint inject + warmup
                await asyncio.sleep(5)
                await asyncio.wait_for(b.navigate("data:text/html;base64," +
                    __import__("base64").b64encode(PROBE_HTML.encode()).decode(),
                    timeout=30000), 45)
                await asyncio.sleep(9)  # ICE gathering + 8s timeout margin
                eval_src = "document.getElementById('out').textContent"
                resp = await asyncio.wait_for(b.send({
                    "method": "Runtime.evaluate",
                    "params": {"expression": eval_src, "returnByValue": True},
                }), 20)
                v = resp.get("result", {}).get("value", "")
                print("=== PAGE REPORT ===")
                print(v)
                r = check_report(v, "candidates")
                sdp_report = json.dumps(r)
                # screenshot as evidence
                try:
                    shot = await asyncio.wait_for(b.screenshot(timeout=30000), 35)
                    data = shot.get("data") if isinstance(shot, dict) else shot
                    if isinstance(data, str):
                        import base64
                        open("/tmp/rtc-spoof-probe.png", "wb").write(base64.b64decode(data))
                        print("screenshot saved /tmp/rtc-spoof-probe.png")
                except Exception as e:
                    print("screenshot failed:", e)
                await b.close()
                # verdict: any fake present AND no bad IP -> PASS
                ok = (len(r["fake"]) > 0) and (len(r["bad"]) == 0)
                print("VERDICT:", "PASS" if ok else "FAIL")
                sys.exit(0 if ok else 2)
        except Exception as e:
            last_err = e
            print("attempt failed:", type(e).__name__, str(e)[:200])
            continue
    print("ALL RENTER TOKENS FAILED:", last_err)
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())