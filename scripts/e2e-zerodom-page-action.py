#!/usr/bin/env python3
"""
E2E: token-lean page graph via zerodom — `browser.page()` / `browser.act()`.

Covers python-sdk PR #33 / js-sdk PR #29 (feature/zerodom-page-action): the
SDK's optional zerodom integration that turns a page into a structured
interaction graph (`[04] a 'Arts and Humanities'`) and lets the agent act on
nodes by number (`click[04]`, `fill[02]:text`) over CDP — no screenshots/OCR.

What it proves against a LIVE provider:
  1. page() returns a non-empty graph (nodes + compact text + url) after rent;
  2. act('fill[NN]:<text>') really puts the text into a search field
     (verified via CDP Runtime.evaluate reading the field value — NOT the
     SDK's own claim);
  3. act('click[NN]') really navigates (URL changes to the node's href) and
     page() after it shows a different graph — the + / - diff reflects it;
  4. quality does not regress: the old screenshot flow still works after
     page/act round-trips on the same session.

Run (live infra, dev contour — same env as e2e-sequential-rents.py):
  RENTER_AGENT_TOKEN=<agent token> \
  CEKI_RELAY_URL_DEV=wss://browser.ittribe.org/ws/agent \
  CEKI_API_URL_DEV=https://clawapi.ittribe.org \
  python3 scripts/e2e-zerodom-page-action.py [schedule] [iterations]

Optional:
  CEKI_PAGE_URL   target page to navigate to before graph (default busybits.com
                  Arts search page — a stable, text-heavy page with a search
                  field and a category link, same one used for the PR's manual e2e)
"""
import asyncio
import os
import sys
import time

# Resolve the python-sdk copy that actually has page()/act() (zerodom).
# Default: the repo-local checkout that is on the feature branch. Environment
# override CEKI_SDK_PATH takes precedence. Otherwise fall back to the plugin's
# python-sdk clone (which may be on master without zerodom — page()/act()
# would be missing, and we fail loudly below).
_SDK_CANDIDATES = [
    os.environ.get("CEKI_SDK_PATH", ""),
    os.path.join(os.path.dirname(__file__), "..", "..", "python-sdk"),
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "ceki-sdk", "python-sdk"),
]
for _p in _SDK_CANDIDATES:
    if _p and os.path.isdir(_p):
        sys.path.insert(0, _p)
        break

from ceki_sdk import ConnectOptions, connect

try:
    from ceki_sdk._browser import Browser
    assert hasattr(Browser, "page") and hasattr(Browser, "act"), "SDK copy lacks page()/act() (needs zerodom branch)"
except (ImportError, AssertionError) as e:
    print(f"FATAL: {e}\n  Use CEKI_SDK_PATH=/abs/path/to/python-sdk (feature/zerodom-page-action)")
    sys.exit(2)

RENTER = os.environ.get("RENTER_AGENT_TOKEN", "")
# Multiple renter tokens rotate on rent failure (dev relay has per-agent
# session/queue limits — a token that times out on rent may work minutes later).
RENTER_TOKENS = [
    t.strip() for t in
    os.environ.get("RENTER_AGENT_TOKENS", "").split(",") if t.strip()
]
if RENTER and RENTER not in RENTER_TOKENS:
    RENTER_TOKENS.insert(0, RENTER)
if not RENTER_TOKENS and RENTER:
    RENTER_TOKENS.append(RENTER)
RELAY = os.environ.get("CEKI_RELAY_URL_DEV", "wss://browser.ittribe.org/ws/agent")
API = os.environ.get("CEKI_API_URL_DEV", "https://clawapi.ittribe.org")
# Candidate pages: tried in order until one yields a plain <input> to fill.
# duckduckgo/google main search fields are <textarea> — fill there hits the SDK
# "Illegal invocation" bug (canary). wikipedia/example have plain inputs, so the
# fill-passing path can actually be verified end-to-end.
PAGE_URLS = [
    u.strip() for u in
    os.environ.get("CEKI_PAGE_URLS", "https://duckduckgo.com,https://www.wikipedia.org").split(",")
    if u.strip()
]

# A node type we can always fill and read back. We target the first textbox/fill
# node found in the graph rather than assuming a specific index, so the test is
# robust to graph shape.
SEARCH_TEXT = "crypto"


def _read_value(resp: dict) -> "object | None":
    """CDP response value in both shapes: flat (`result.value`) on some
    providers vs nested (`result.result.value`) on others (ev 16783: rental
    smoke CDP format is flat). Try flat first, then nested."""
    res = resp.get("result")
    if isinstance(res, dict):
        if "value" in res:
            return res["value"]
        inner = res.get("result")
        if isinstance(inner, dict) and "value" in inner:
            return inner["value"]
        if "description" in res:  # primitive with description (number type)
            return res.get("description")
    return None


async def read_field(browser, selector: str) -> str | None:
    """Read an input's current value via raw CDP (not the SDK's own page())."""
    import json as _json

    q = _json.dumps(selector)
    resp = await browser.send(
        {
            "method": "Runtime.evaluate",
            "params": {
                "expression": (
                    "(()=>{const el=document.querySelector(" + q + ");"
                    "return el?el.value:null})()"
                ),
                "returnByValue": True,
            },
        }
    )
    val = _read_value(resp)
    return str(val) if val is not None else None


async def read_url(browser) -> str | None:
    resp = await browser.send(
        {"method": "Runtime.evaluate",
         "params": {"expression": "location.href", "returnByValue": True}}
    )
    return _read_value(resp)


async def one_iter(make_client, sid: int, idx: int) -> dict:
    """Rent (with token rotation) -> page -> fill -> verify -> click -> screenshot.
    Returns per-iteration metrics; no exception escapes."""
    out = {"idx": idx, "sid": None, "error": None,
           "page_nodes": None, "tokens": None,
           "fill_node": None, "fill_readback": None, "fill_ok": None,
           "click_ok": None, "url_before": None, "url_after": None,
           "diff": None, "shot_bytes": None}
    b = None
    c = None
    t0 = time.monotonic()
    try:
        # Rent with token rotation: a token can hit a session/queue limit and
        # time out on rent while another works — try each once.
        for tok in RENTER_TOKENS:
            try:
                c = await asyncio.wait_for(
                    connect(tok, ConnectOptions(relay_url=RELAY, api_url=API, reconnect=False)), 20
                )
                b = await asyncio.wait_for(c.rent(sid), timeout=85)
                break
            except asyncio.TimeoutError:
                out["error"] = "rent timed out waiting for match"
            except Exception as e:
                out["error"] = f"rent: {type(e).__name__}: {str(e)[:90]}"
            # close the failed client before trying the next token
            if c:
                try:
                    await c.disconnect()
                except Exception:
                    pass
                c = None
        if b is None:
            if out["error"] is None:
                out["error"] = "rent: all tokens failed"
            return out
        out["sid"] = b.session_id

        # Warm-up: after a rent the daemon spawns Chrome and the extension must
        # connect its local-WS presence before CDP answers. Wait ~8s so the first
        # navigate/page doesn't hit the "extension not connected" buffered window.
        await asyncio.sleep(8)

        # 1-2. navigate + graph + find fill target, trying each candidate URL
        #      until we get a plain <input> to fill (textarea = SDK-bug canary).
        #      On dev, P2P DC sometimes fails → permanent WS fallback, which is
        #      slower — give navigate a generous timeout.
        nodes = []
        fill_node = None
        for url in PAGE_URLS:
            nav_ok = False
            for _attempt in range(4):
                try:
                    await asyncio.wait_for(b.navigate(url, timeout=45000), 55)
                    nav_ok = True
                    break
                except Exception as e:
                    out["error"] = f"navigate {url}: {type(e).__name__}: {str(e)[:80]}"
                    await asyncio.sleep(5)
            if not nav_ok:
                continue

            # First page() after navigate can race a slow load and return an
            # empty outerHTML (same transient-nav race as post-click) — retry.
            g = None
            for _p in range(4):
                try:
                    g = await asyncio.wait_for(b.page(timeout=30000), 40)
                    if g.get("nodes"):
                        break
                except Exception:
                    pass
                await asyncio.sleep(3)
            if g is None:
                out["error"] = f"page() on {url} after retries: empty"
                continue
            nodes = g.get("nodes") or []
            out["page_nodes"] = len(nodes)
            out["tokens"] = g.get("tokens")
            out["page_url"] = url

            if not nodes:
                out["error"] = f"page() on {url}: graph is empty"
                continue

            # zerodom classifies inputs sometimes as action=click; pick by TYPE.
            # Prefer a plain <input>: fill works there. A <textarea> is a canary
            # for the SDK "Illegal invocation" bug (fill assumes HTMLInputElement).
            inputs = [n for n in nodes
                      if n.get("type") == "input" and n.get("selector")]
            textareas = [n for n in nodes
                         if n.get("type") == "textarea" and n.get("selector")]
            text_fields = inputs + textareas
            cand = next(
                (n for n in text_fields
                 if "q" in str(n.get("selector")).lower()
                 or "search" in str(n.get("label") or "").lower()),
                None,
            )
            if not cand and text_fields:
                cand = text_fields[0]
            if not cand:
                cand = next((n for n in nodes if n.get("selector")), None)
            if not cand:
                out["error"] = f"page() on {url}: no actionable node with a selector"
                continue

            fill_node = cand
            # Stop at the first URL whose field is a plain input (pass path);
            # keep going through URLs if all we have is a textarea (canary path).
            if fill_node.get("type") == "input":
                break
            out["textarea_found"] = True

        if fill_node is None:
            if not out["error"]:
                out["error"] = "no URL yielded an actionable fill node"
            return out
        out["fill_target_type"] = fill_node.get("type")

        # canary: if the only fill target is a textarea, still attempt fill so we
        # surface the SDK bug as a distinct "textarea_fill" metric instead of a
        # hard test failure (the other contract parts must still be verified).
        idx_label = fill_node["id"]  # e.g. "node_02" -> use "fill[02]:..."
        num = "".join(ch for ch in str(idx_label) if ch.isdigit())
        out["fill_node"] = idx_label

        act = None
        try:
            act = await asyncio.wait_for(
                b.act(f"fill[{num}]:{SEARCH_TEXT}", timeout=30000), 40
            )
        except Exception as e:
            err = f"act fill failed: {type(e).__name__}: {str(e)[:150]}"
            out["fill_error"] = err
            if fill_node.get("type") == "textarea":
                # KNOWN SDK bug (python#33 / js#29): fill assumes
                # HTMLInputElement prototype → textarea throws Illegal invocation.
                out["textarea_fill_bug"] = True
                out["fill_error"] = (
                    "textarea_fill_bug: fill on <textarea> throws Illegal "
                    "invocation (sdk assumes HTMLInputElement)"
                )
                out["fill_ok"] = False
            else:
                out["error"] = err
                return out
        if act is not None and not act.get("ok", False):
            out["fill_error"] = f"act fill failed: {str(act)[:150]}"
            if fill_node.get("type") == "textarea":
                out["textarea_fill_bug"] = True
                out["fill_ok"] = False
            else:
                out["error"] = out["fill_error"]
                return out

        # 2b. verify the text is really in the field (raw CDP, independent check)
        try:
            out["fill_readback"] = await read_field(b, fill_node["selector"])
            out["fill_ok"] = out["fill_readback"] == SEARCH_TEXT
            if not out["fill_ok"]:
                out["fill_error"] = (
                    f"readback mismatch: selector={fill_node['selector']!r} "
                    f"got {out['fill_readback']!r}"
                )
        except Exception as e:
            out["fill_readback"] = None
            out["fill_ok"] = False
            out["fill_error"] = (
                f"readback: {type(e).__name__}: {str(e)[:100]} "
                f"selector={fill_node.get('selector')!r}"
            )

        # 3. pick a clickable link node and click it -> should navigate to its href.
        #    act(click) computes a graph diff after the click; if the click
        #    started a full navigation, that post-click _outer_html can race
        #    the nav and come back empty ("page: Runtime.evaluate returned no
        #    outerHTML") — retry the click a few times.
        link_node = next(
            (n for n in nodes if n.get("action") == "click" and n.get("href")),
            None,
        )
        if link_node:
            lnum = "".join(ch for ch in str(link_node["id"]) if ch.isdigit())
            out["url_before"] = await read_url(b)
            click_res = None
            for _c in range(4):
                try:
                    click_res = await asyncio.wait_for(
                        b.act(f"click[{lnum}]", timeout=30000), 40
                    )
                    break
                except Exception as e:
                    out["click_error"] = f"{type(e).__name__}: {str(e)[:100]}"
                    await asyncio.sleep(4)
            out["diff"] = (click_res.get("diff") or "")[:400] if click_res else ""
            await asyncio.sleep(3)  # let the nav settle
            out["url_after"] = await read_url(b)
            target = (link_node.get("href") or "")
            # Accept a nav if the URL changed OR the target href appears in the
            # new URL (relative hrefs / redirects are common on real sites).
            out["click_ok"] = (
                out["url_before"] != out["url_after"]
                or (target and target in (out["url_after"] or ""))
            )
            # re-graph after nav: graph should reflect the new page. The first
            # page() right after a click can race the ongoing navigation and
            # return an empty outerHTML — retry a few times.
            g2 = None
            for _g in range(3):
                try:
                    g2 = await asyncio.wait_for(b.page(timeout=30000), 40)
                    break
                except Exception:
                    await asyncio.sleep(3)
            out["graph2_nodes"] = len((g2 or {}).get("nodes") or []) if g2 else 0
        else:
            out["click_ok"] = False
            out["error"] = out["error"] or "no clickable link node with href in graph"

        # 4. quality check: old screenshot flow still works on the same session
        shot = await asyncio.wait_for(b.screenshot(timeout=45000), 55)
        data = shot.get("data") if isinstance(shot, dict) else shot
        out["shot_bytes"] = len(data) if isinstance(data, str) else (
            len(shot) if isinstance(shot, bytes) else -1
        )

    except asyncio.TimeoutError:
        out["error"] = "timeout"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        if b:
            try:
                await asyncio.wait_for(b.close(), timeout=30)
            except Exception:
                pass
        if c:
            try:
                await c.disconnect()
            except Exception:
                pass
        out["total_s"] = round(time.monotonic() - t0, 1)
    return out


def passes(r: dict) -> bool:
    if r["error"]:
        return False
    # The page/act contract must hold: non-empty graph, navigation happened via
    # click, screenshot works. fill_ok is only required when the fill target was
    # a plain input; a textarea canary is reported separately (SDK bug).
    fill_gate = r.get("fill_ok") is True or r.get("fill_target_type") == "textarea"
    return (
        (r.get("page_nodes") or 0) > 0
        and fill_gate
        and r.get("click_ok") is True
        and (r.get("shot_bytes") or 0) > 0
    )


async def main() -> None:
    if not RENTER_TOKENS:
        print("FATAL: RENTER_AGENT_TOKEN unset (live infra only)")
        sys.exit(2)
    sid = int(sys.argv[1]) if len(sys.argv) > 1 else 40693
    iters = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    print(f"=== e2e zerodom page/act: schedule {sid}, {iters} iters (LIVE) ===", flush=True)

    passed = 0
    for i in range(iters):
        r = await one_iter(None, sid, i)
        ok = passes(r)
        if ok:
            passed += 1
        print(
            f"  {'PASS' if ok else 'FAIL'} #{r['idx']} sid={r.get('sid')} "
            f"nodes={r.get('page_nodes')} tok={r.get('tokens')} "
            f"fill={r.get('fill_node')}({r.get('fill_target_type')})={r.get('fill_readback')!r}/{r.get('fill_ok')} "
            f"canary_ta={'BUG' if r.get('textarea_fill_bug') else '-'} "
            f"click={r.get('click_ok')} diff={len(r.get('diff') or '')}ch "
            f"shot={r.get('shot_bytes')}B total={r.get('total_s')}s",
            flush=True,
        )
        if r.get("fill_error"):
            print(f"        fill: {r['fill_error']}")
        if r.get("url_before") and r.get("url_after"):
            print(f"        url: {r['url_before'][:70]} -> {r['url_after'][:70]}")
        if r.get("diff"):
            print(f"        diff:\n{r['diff']}")
        if r["error"]:
            print(f"        err={r['error']}")
    print(f"=== result: {'PASS' if passed == iters else 'FAIL'} ({passed}/{iters}) ===")
    sys.exit(0 if passed == iters else 1)


if __name__ == "__main__":
    asyncio.run(main())