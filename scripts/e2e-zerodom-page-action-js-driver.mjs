// E2E: js-sdk zerodom page()/act() against a live provider (Node driver).
// Uses @ceki/sdk node entry + connect(agentToken) — same contract as the
// python e2e. Run by e2e-zerodom-page-action-js.py. The SDK dist path comes
// from CEKI_JS_INDEX (default: ../../js-sdk/dist/index.js relative to caller).
import { pathToFileURL } from 'node:url';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { existsSync } from 'node:fs';

const distIndex = process.env.CEKI_JS_INDEX
  || resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'js-sdk', 'dist', 'index.js');
if (!existsSync(distIndex)) {
  console.error('FATAL: js-sdk dist not found:', distIndex, '(set CEKI_JS_INDEX or build js-sdk)');
  process.exit(2);
}
const { connect } = await import(pathToFileURL(distIndex).href);

const RELAY = process.env.CEKI_RELAY_URL_DEV || 'wss://browser.ittribe.org/ws/agent';
const API = process.env.CEKI_API_URL_DEV || 'https://clawapi.ittribe.org';
const TOKENS = (process.env.RENTER_AGENT_TOKENS || '').split(',').map(s => s.trim()).filter(Boolean);
const SID = parseInt(process.argv[2] || '40693', 10);
const PAGE_URLS = (process.env.CEKI_PAGE_URLS || 'https://www.wikipedia.org').split(',').map(s => s.trim()).filter(Boolean);
const SEARCH = 'crypto';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function readValue(resp) {
  const res = resp?.result;
  if (res && typeof res === 'object') {
    if ('value' in res) return res.value;
    if (res.result && typeof res.result === 'object' && 'value' in res.result) return res.result.value;
  }
  return undefined;
}

async function readField(browser, selector) {
  const r = await browser.send({ method: 'Runtime.evaluate', params: { expression: `(()=>{const el=document.querySelector(${JSON.stringify(selector)});return el?el.value:null})()`, returnByValue: true } });
  return readValue(r);
}

async function readUrl(browser) {
  const r = await browser.send({ method: 'Runtime.evaluate', params: { expression: 'location.href', returnByValue: true } });
  return readValue(r);
}

async function oneIter(idx) {
  const out = { idx, sid: null, error: null };
  let client = null, b = null;
  const t0 = Date.now();
  try {
    // rent with token rotation
    let rentErr = null;
    for (const tok of TOKENS) {
      try {
        client = await connect(tok, { relayUrl: RELAY, apiUrl: API, reconnect: false });
        b = await Promise.race([
          client.rent(SID, {}),
          new Promise((_, rej) => setTimeout(() => rej(new Error('rent timeout')), 85000)),
        ]);
        break;
      } catch (e) { rentErr = `${e.name||e.constructor?.name}: ${String(e.message||e).slice(0,90)}`; if (client) { try { await client.disconnect(); } catch {} } client = null; }
    }
    if (!b) { out.error = rentErr || 'rent: all tokens failed'; return out; }
    out.sid = b.sessionId || null;

    await sleep(8000); // extension presence-WS warmup

    // navigate + graph, retry
    let nodes = [], fill = null, lastErr = null;
    for (const url of PAGE_URLS) {
      let navOk = false;
      for (let a = 0; a < 3; a++) {
        try { await Promise.race([b.navigate(url, 45000), new Promise((_,r)=>setTimeout(()=>r(),55000))]); navOk = true; break; }
        catch (e) { lastErr = `navigate ${url}: ${e.name}: ${String(e.message||e).slice(0,80)}`; await sleep(4000); }
      }
      if (!navOk) continue;
      let g = null;
      for (let p = 0; p < 4; p++) {
        try { g = await Promise.race([b.page(), new Promise((_,r)=>setTimeout(()=>r(),40000))]); if (g?.nodes?.length) break; } catch {}
        await sleep(3000);
      }
      if (!g?.nodes?.length) { lastErr = `page() ${url}: empty graph`; continue; }
      nodes = g.nodes;
      out.page_nodes = nodes.length;
      out.tokens = g.tokens;
      const textFields = nodes.filter(n => (n.type === 'input' || n.type === 'textarea') && n.selector);
      const cand = textFields.find(n => /search/i.test(String(n.label||'')) || /q/.test(String(n.selector||''))) || textFields[0];
      if (cand) { fill = cand; if (fill.type === 'input') break; }
    }
    if (!fill) { out.error = lastErr || 'no fillable node'; return out; }
    out.fill_target_type = fill.type;
    const num = String(fill.id).replace(/\D/g, '');
    out.fill_node = fill.id;
    out.fill_selector = fill.selector;

    try { const a = await Promise.race([b.act(`fill[${num}]:${SEARCH}`), new Promise((_,r)=>setTimeout(()=>r(),40000))]); out.fill_act_ok = !!a?.ok; }
    catch (e) { out.fill_act_ok = false; out.fill_error = `${e.name}: ${String(e.message||e).slice(0,120)}`; }
    out.fill_readback = await readField(b, fill.selector);
    out.fill_ok = out.fill_readback === SEARCH;

    // click a link
    const link = nodes.find(n => n.action === 'click' && n.href);
    if (link) {
      const lnum = String(link.id).replace(/\D/g, '');
      out.url_before = await readUrl(b);
      for (let c = 0; c < 4; c++) {
        try { await Promise.race([b.act(`click[${lnum}]`), new Promise((_,r)=>setTimeout(()=>r(),40000))]); break; }
        catch (e) { out.click_error = `${e.name}: ${String(e.message||e).slice(0,80)}`; await sleep(3000); }
      }
      await sleep(3000);
      out.url_after = await readUrl(b);
      out.click_ok = out.url_before !== out.url_after;
    } else out.click_ok = false;

    // screenshot
    try { const s = await Promise.race([b.screenshot(), new Promise((_,r)=>setTimeout(()=>r(),55000))]); const data = s?.data ?? s; out.shot_bytes = typeof data === 'string' ? data.length : -1; }
    catch (e) { out.shot_bytes = -1; out.shot_error = `${e.name}: ${String(e.message||e).slice(0,80)}`; }

  } catch (e) { out.error = `${e.name}: ${String(e.message||e).slice(0,160)}`; }
  finally {
    if (b) { try { await Promise.race([b.close(), new Promise((_,r)=>setTimeout(()=>r(),30000))]); } catch {} }
    if (client) { try { await client.disconnect(); } catch {} }
    out.total_s = Math.round((Date.now() - t0) / 1000);
  }
  return out;
}

function passes(r) { return !r.error && r.page_nodes > 0 && r.fill_ok === true && r.click_ok === true && (r.shot_bytes||0) > 0; }

const ITERS = parseInt(process.argv[3] || '2', 10);
console.log(`=== e2e js-sdk zerodom page/act: schedule ${SID}, ${ITERS} iters (LIVE) ===`);
let passed = 0;
for (let i = 0; i < ITERS; i++) {
  const r = await oneIter(i);
  const ok = passes(r); if (ok) passed++;
  console.log(`  ${ok?'PASS':'FAIL'} #${r.idx} sid=${r.sid} nodes=${r.page_nodes} tok=${r.tokens} fill=${r.fill_node}(${r.fill_target_type})=${JSON.stringify(r.fill_readback)}/${r.fill_ok} click=${r.click_ok} shot=${r.shot_bytes}B total=${r.total_s}s`);
  if (r.url_before && r.url_after) console.log(`        url: ${String(r.url_before).slice(0,70)} -> ${String(r.url_after).slice(0,70)}`);
  for (const k of ['fill_error','click_error','shot_error']) if (r[k]) console.log(`        ${k}: ${r[k]}`);
  if (r.error) console.log(`        err=${r.error}`);
}
console.log(`=== result: ${passed===ITERS?'PASS':'FAIL'} (${passed}/${ITERS}) ===`);
process.exit(passed===ITERS?0:1);