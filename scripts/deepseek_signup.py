#!/usr/bin/env python3
"""
DeepSeek web signup E2E test — rent a browser, register an account by email,
save the working profile (cookies+storage) per run.

QA test-case (contract task 14657): build a reserve of DeepSeek accounts.
Each successful signup exports a working profile (cookies + localStorage +
sessionStorage) into <DEEPSEEK_PROFILE_DIR>/signup-<ts>/, so the account can
be resumed/logged in later.

Disposable email: uses mailticking.com via its internal API. Because the
host egress IP is a datacenter (Cloudflare blocks it), the mailticking API is
called through a residential/RU proxy — pass CEKI_PROXY_URL (http://user:pass@host:port).
The rented RU browser also exits through that proxy (provider env), so both
DeepSeek and mailticking see a consistent RU IP.

Flow:
  1. rent a browser (RU schedule by default; provider must egress via the proxy)
  2. get a disposable address from mailticking (through the proxy)
  3. open chat.deepseek.com/sign_up
  4. fill Email address / Password / Confirm password
  5. click "Send code" (DeepSeek emails a 6-digit code)
  6. poll mailticking for the email, extract the 6-digit code
  7. enter the code, confirm, land in the chat
  8. export the working profile (cookies+storage) + screenshot to a per-run dir

Known env-detection blocker (task 14652/14654): DeepSeek can reject with
"Current device environment error" when WebRTC exposes the docker bridge IP
(172.x) or tz/locale mismatch the geo. The script prints a diagnostic probe
(WebRTC candidates + timezone + language) so the report includes the exact
signature, and still exports whatever profile exists.

No internal endpoints/tokens hardcoded — everything from env (public repo).

Run:
  CEKI_AGENT_TOKEN=<ag_...> \
  CEKI_PROXY_URL=http://user:pass@host:port \
  python3 scripts/deepseek_signup.py [--schedule 43666] [--count 3]

Optional env:
  DEEPSEEK_PROFILE_DIR   where per-run profile folders go (default ./deepseek-profiles)
  DEEPSEEK_SIGNUP_PASS   signup password (default random)
  CEKI_API_URL / CEKI_RELAY_URL / CEKI_CHAT_URL  contour endpoints
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as _dt
import json
import os
import random
import re
import string
import sys
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

API = os.environ.get("CEKI_API_URL", "https://api.ceki.me")
RELAY = os.environ.get("CEKI_RELAY_URL", "wss://browser.ceki.me/ws/agent")
CHAT = os.environ.get("CEKI_CHAT_URL", "https://chat.ceki.me/api/chat")
PROFILE_DIR = os.environ.get("DEEPSEEK_PROFILE_DIR", "deepseek-profiles")
PROXY_URL = os.environ.get("CEKI_PROXY_URL", "")  # http://user:pass@host:port

import ceki_sdk

_CODE_RE = re.compile(r"(\b\d{6}\b)")
MAILTICKING_BASE = "https://www.mailticking.com"
_CONFIRM_BTN = (
    "(()=>{const all=[...document.querySelectorAll('[role=button],button,.ds-button')];"
    "const btn=all.filter(e=>e.getBoundingClientRect().width>0).find(e=>"
    "(e.textContent||'').trim().toLowerCase()==='confirm'||(e.textContent||'').trim()==='Confirm');"
    "if(btn){btn.click();return 'clicked'}return 'nf'})()"
)


class MailTicking:
    """Disposable mailbox via mailticking.com internal API (through a proxy)."""

    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

    def __init__(self) -> None:
        self._email = ""
        self._code = ""
        self._opener: urllib.request.OpenerDirector | None = None

    def _open(self, path: str, data: dict | None = None, headers: dict | None = None) -> tuple[int, str]:
        if self._opener is None:
            self._cj = CookieJar()
            handlers: list = [urllib.request.HTTPCookieProcessor(self._cj)]
            if PROXY_URL:
                handlers.append(urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL}))
            self._opener = urllib.request.build_opener(*handlers)
        url = MAILTICKING_BASE + path
        h = {"User-Agent": self.UA, "Accept": "application/json", "Content-Type": "application/json",
             "Referer": MAILTICKING_BASE + "/"}
        if headers:
            h.update(headers)
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=h, method="POST" if data is not None else "GET")
        try:
            with self._opener.open(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")

    def new_address(self) -> str:
        """Create a disposable address; returns the email. Activate + fetch data-code."""
        status, body = self._open("/get-mailbox", {"types": ["1", "2", "3", "4"]})
        if status != 200:
            raise RuntimeError(f"mailticking get-mailbox HTTP {status}: {body[:200]}")
        d = json.loads(body)
        if not d.get("success") or not d.get("email"):
            raise RuntimeError(f"mailticking get-mailbox failed: {body[:200]}")
        self._email = d["email"]
        token = d.get("activate_token", "")
        # Activate the mailbox, then fetch the homepage to obtain data-code.
        status, body = self._open("/activate-email", {"email": self._email, "source": "homepage", "activate_token": token})
        status, body = self._open("/")
        m = re.search(r'data-code="([^"]+)"', body)
        self._code = m.group(1) if m else ""
        return self._email

    async def poll_code(self, timeout: float = 180, poll_interval: float = 5) -> str:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            status, body = await asyncio.to_thread(self._open, "/get-emails?lang=en", {"email": self._email, "code": self._code})
            if status == 200:
                try:
                    d = json.loads(body)
                    for em in d.get("emails", []):
                        text = str(em.get("text", "")) + "\n" + str(em.get("html", ""))
                        m = _CODE_RE.search(text)
                        if m:
                            return m.group(1)
                except Exception:
                    pass
            await asyncio.sleep(poll_interval)
        raise TimeoutError(f"No DeepSeek code on {self._email} within {timeout}s")


async def ev(browser, expr, tmo=50):
    r = await browser.send(
        {"method": "Runtime.evaluate", "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}},
        timeout=tmo,
    )
    if "exceptionDetails" in r:
        return None
    return r.get("result", {}).get("value")


def fill_expr(email_addr: str, pwd: str) -> str:
    return f"""(()=>{{
      const set=(ph,val)=>{{const i=[...document.querySelectorAll('input')].find(x=>x.placeholder===ph);if(!i)return ph+':missing';const proto=HTMLInputElement.prototype;const desc=Object.getOwnPropertyDescriptor(proto,'value');desc.set.call(i,val);i.dispatchEvent(new Event('input',{{bubbles:true}}));i.dispatchEvent(new Event('change',{{bubbles:true}}));return ph+':ok'}};
      return [set('Email address','{email_addr}'),set('Password','{pwd}'),set('Confirm password','{pwd}')].join(' | ');
    }})()"""


SEND_CODE = (
    "(()=>{const all=[...document.querySelectorAll('[role=button],button,.ds-button')];"
    "const btn=all.filter(e=>e.getBoundingClientRect().width>0).find(e=>"
    "(e.textContent||'').trim()==='Send code');if(btn){btn.click();return 'clicked'}return 'nf'})()"
)


async def web_rtc_probe(browser) -> str:
    return await ev(
        browser,
        """(async()=>{
          const pc=new RTCPeerConnection({iceServers:[]});const c=[];
          pc.onicecandidate=e=>{if(e.candidate)c.push(e.candidate.candidate)};
          pc.createDataChannel('x');
          await pc.setLocalDescription(await pc.createOffer());
          await new Promise(r=>setTimeout(r,1500));pc.close();
          return JSON.stringify({cands:c,tz:Intl.DateTimeFormat().resolvedOptions().timeZone,lang:navigator.language});
        })()""",
    )


async def save_profile(browser, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    try:
        profile = await browser.profile.export()
        with open(os.path.join(out_dir, "profile.json"), "w") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        print(f"[ds-signup] profile export warn: {e}")
    try:
        png = await browser.screenshot(format="png")
        if isinstance(png, dict):
            png = png.get("data") or png.get("png") or png.get("base64")
        if isinstance(png, str):
            png = base64.b64decode(png)
        if png:
            with open(os.path.join(out_dir, "signup.png"), "wb") as f:
                f.write(png)
    except Exception as e:
        print(f"[ds-signup] screenshot warn: {e}")


async def run_one(client, args) -> int:
    pwd = args.password or "".join(random.choices(string.ascii_letters + string.digits, k=16)) + "!Aa"
    sid = None
    try:
        mt = MailTicking()
        email_addr = mt.new_address()
        print(f"[ds-signup] mailticking addr={email_addr}")

        browser = await asyncio.wait_for(client.rent(args.schedule, mode="incognito", fingerprint=True), timeout=90)
        sid = browser.session_id
        print(f"[ds-signup] rented sid={sid} schedule={args.schedule}")

        await asyncio.sleep(2)
        await asyncio.wait_for(browser.navigate("https://chat.deepseek.com/sign_up", timeout=50), timeout=60)
        await asyncio.sleep(8)
        print(f"[ds-signup] url={await ev(browser, 'location.href')}")

        probe = await web_rtc_probe(browser)
        print(f"[ds-signup] probe={probe}")

        print(f"[ds-signup] fill: {await ev(browser, fill_expr(email_addr, pwd))}")
        await asyncio.sleep(1)
        print(f"[ds-signup] send: {await ev(browser, SEND_CODE)}")

        code = await mt.poll_code()
        print(f"[ds-signup] got code={code}")
        enter_code = f"""(()=>{{
          const inputs=[...document.querySelectorAll('input')].filter(i=>i.type==='text'||i.type==='tel'||!i.type);
          if(inputs.length===6){{inputs.forEach((i,idx)=>{{const proto=HTMLInputElement.prototype;const desc=Object.getOwnPropertyDescriptor(proto,'value');desc.set.call(i,'{code}'[idx]);i.dispatchEvent(new Event('input',{{bubbles:true}}));}});return 'six'}}
          const one=inputs.find(i=>i.maxLength>=6||(i.placeholder||'').toLowerCase().includes('code'));
          if(one){{const proto=HTMLInputElement.prototype;const desc=Object.getOwnPropertyDescriptor(proto,'value');desc.set.call(one,'{code}');one.dispatchEvent(new Event('input',{{bubbles:true}}));one.dispatchEvent(new Event('change',{{bubbles:true}}));return 'one'}}
          return 'nf:'+inputs.length;
        }})()"""
        print(f"[ds-signup] code enter: {await ev(browser, enter_code)}")
        await asyncio.sleep(1)
        print(f"[ds-signup] confirm: {await ev(browser, _CONFIRM_BTN)}")
        await asyncio.sleep(6)

        txt = await ev(browser, "document.body.innerText.slice(0,1500)") or ""
        env_err = "environment error" in txt.lower()
        logged_in = ("chat.deepseek.com" in str(await ev(browser, "location.href"))) or ("new chat" in txt.lower())
        print(f"[ds-signup] env_err={env_err} logged_in={logged_in}")

        ts = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        out_dir = os.path.join(PROFILE_DIR, f"signup-{ts}")
        await save_profile(browser, out_dir)
        print(f"[ds-signup] profile saved to {out_dir}")

        if env_err:
            return 2  # env-detection blocker (14652/14654) still active
        return 0 if logged_in else 1
    finally:
        if sid:
            try:
                b = await asyncio.wait_for(client.resume(sid), timeout=20)
                await b.release()
                print(f"[ds-signup] released {sid}")
            except Exception:
                pass


async def main(args):
    tok = os.environ.get("CEKI_AGENT_TOKEN", "")
    if not tok:
        sys.exit("CEKI_AGENT_TOKEN not set")
    if not PROXY_URL:
        print("WARN: CEKI_PROXY_URL not set — mailticking will likely be blocked from a datacenter IP")
    client = ceki_sdk.Client(tok, relay_url=RELAY, api_url=API, chat_url=CHAT, reconnect=False)
    await client._connect()
    fails = 0
    for i in range(args.count):
        print(f"\n===== run {i + 1}/{args.count} =====")
        try:
            rc = await asyncio.wait_for(run_one(client, args), timeout=400)
        except Exception as e:
            print(f"[ds-signup] run {i + 1} error: {type(e).__name__}: {e}")
            rc = 1
        if rc != 0:
            fails += 1
        await asyncio.sleep(5)
    await client.disconnect()
    print(f"=== deepseek signup result: {args.count - fails}/{args.count} ok ===")
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule", type=int, default=int(os.environ.get("DEEPSEEK_SCHEDULE", "43666")))
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--password", default="")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args)))
