#!/usr/bin/env node
/**
 * e2e-multirent-app.mjs — multi-rent in APP mode (parallel concurrent rents), JS twin of
 * docker-browser/scripts/e2e-multirent-app.py
 *
 * Several agents rent the SAME schedule CONCURRENTLY through the real relay,
 * each driving its own session. This is the scenario the presence-WS connect
 * fix (config-resolved guard in offscreen/ws.ts) must not break.
 *
 * Usage (live infra):
 *   RENTER_AGENT_TOKEN=<token> node scripts/e2e-multirent-app.mjs [schedule] [count]
 *   or, with per-renter tokens:
 *   RENTER_TOKENS="tok1 tok2 tok3" node scripts/e2e-multirent-app.mjs 42920 3
 *
 * Env overrides (dev host by default):
 *   CEKI_RELAY_URL_DEV, CEKI_API_URL_DEV
 */
import { connect } from '/home/node/.openclaw/ceki-plugin/js-sdk/dist/index.js';
import { readFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function resolveToken(name) {
  for (const f of [
    path.join(__dirname, '..', '.env'),
    `/home/node/.openclaw/secrets/${name}.txt`,
  ]) {
    if (existsSync(f)) {
      const raw = readFileSync(f, 'utf8');
      for (const line of raw.split('\n')) {
        const m = line.match(/^\s*(?:export\s+)?CEKI_RENTER_TOKEN\s*=\s*(.+)\s*$/);
        if (m) return m[1].trim();
      }
    }
  }
  return '';
}

const RENTER = process.env.RENTER_AGENT_TOKEN || resolveToken('skill_rent_agent_token');
const RENTER_TOKENS = (process.env.RENTER_TOKENS || '').trim().split(/\s+/).filter(Boolean);
const RELAY = process.env.CEKI_RELAY_URL_DEV || 'wss://browser.ittribe.org/ws/agent';
const API = process.env.CEKI_API_URL_DEV || 'https://clawapi.ittribe.org';

const RELAY_WS = RELAY.startsWith('ws') ? RELAY : `wss://${RELAY}`;
const API_HTTP = API.startsWith('http') ? API : `https://${API}`;

async function oneRent(sid, idx, renter) {
  const t0 = Date.now();
  const out = { idx, sid: null, renter: renter.slice(0, 6) + '…', rent_ms: null, nav: null, shot_bytes: null };
  let c = null, b = null;
  try {
    c = await Promise.race([
      connect(renter, { relayUrl: RELAY_WS, apiUrl: API_HTTP, reconnect: false }),
      new Promise((_, rej) => setTimeout(() => rej(new Error('connect timeout 20s')), 20000)),
    ]);
    b = await Promise.race([
      c.rent(sid),
      new Promise((_, rej) => setTimeout(() => rej(new Error('rent timeout 90s')), 90000)),
    ]);
    out.sid = b.sessionId ?? b.session_id ?? null;
    out.rent_ms = Date.now() - t0;
    await Promise.race([
      b.navigate('https://example.com', 30000),
      new Promise((_, rej) => setTimeout(() => rej(new Error('navigate timeout 40s')), 40000)),
    ]);
    out.nav = 'ok';
    try {
      let shot = await Promise.race([
        b.screenshot({ timeout: 45000 }),
        new Promise((_, rej) => setTimeout(() => rej(new Error('screenshot timeout 52s')), 52000)),
      ]);
      // retry once — 3 concurrent captures can momentarily stall a render
      if (!shot || (typeof shot === 'object' && !shot.data && !shot.buffer && shot.length === 0)) {
        shot = await Promise.race([
          b.screenshot({ timeout: 45000 }),
          new Promise((_, rej) => setTimeout(() => rej(new Error('screenshot retry timeout 52s')), 52000)),
        ]);
      }
      if (typeof shot === 'string') out.shot_bytes = shot.length;
      else if (shot && shot.data) out.shot_bytes = String(shot.data).length;
      else if (shot && typeof shot.pipe === 'function') out.shot_bytes = shot.length ?? -1;
      else if (Buffer.isBuffer(shot)) out.shot_bytes = shot.length;
      else if (shot && shot.buffer) out.shot_bytes = shot.buffer.length;
      else if (Array.isArray(shot)) out.shot_bytes = shot.length;
      else out.shot_bytes = -1;
    } catch (e) {
      out.error = `${e.constructor.name}: ${String(e.message).slice(0, 120)}`;
      out.shot_bytes = null;
    }
  } catch (e) {
    out.error = `${e.constructor.name}: ${String(e.message).slice(0, 120)}`;
  } finally {
    if (b) { try { await b.close(); } catch {} }
    if (c) { try { await c.disconnect(); } catch {} }
    out.total_ms = Date.now() - t0;
  }
  return out;
}

async function main() {
  if (!RENTER && RENTER_TOKENS.length === 0) {
    console.error('FATAL: RENTER_AGENT_TOKEN or RENTER_TOKENS unset (live infra only)');
    process.exit(2);
  }
  const sid = Number(process.argv[2] ?? 42306);
  const n = Number(process.argv[3] ?? 3);
  let renters;
  if (RENTER_TOKENS.length) {
    if (RENTER_TOKENS.length < n) {
      console.error(`FATAL: RENTER_TOKENS has ${RENTER_TOKENS.length} tokens, need ${n}`);
      process.exit(2);
    }
    renters = RENTER_TOKENS.slice(0, n);
  } else {
    renters = Array(n).fill(RENTER);
  }
  console.log(`=== e2e-multirent APP-mode (JS): ${n} concurrent rents, ${new Set(renters).size} distinct renter(s), schedule ${sid} (LIVE) ===`);
  const start = Date.now();
  const results = await Promise.all(renters.map((r, i) => oneRent(sid, i, r)));
  const wall = Date.now() - start;

  let passed = 0;
  for (const r of results.sort((a, b) => (a.error != null) - (b.error != null) || a.idx - b.idx)) {
    const ok = r.nav === 'ok' && r.shot_bytes != null && r.shot_bytes > 1000;
    passed += ok ? 1 : 0;
    console.log(`  ${ok ? 'PASS' : 'FAIL'} #${r.idx} renter=${r.renter} sid=${r.sid} rent=${r.rent_ms != null ? (r.rent_ms / 1000).toFixed(1) : 'N/A'}s nav=${r.nav} shot=${r.shot_bytes}B total=${(r.total_ms / 1000).toFixed(1)}s` + (r.error ? ` err=${r.error}` : ''));
  }
  console.log(`=== result: ${passed === n ? 'PASS' : 'FAIL'} (${passed}/${n} concurrent rents ok) wall=${(wall / 1000).toFixed(1)}s ===`);
  process.exit(passed === n ? 0 : 1);
}

main().catch((e) => { console.error('FATAL:', e); process.exit(1); });