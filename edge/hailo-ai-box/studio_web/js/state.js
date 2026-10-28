// Shared live state: system snapshot stream, inspector feed, DOM helpers.
import { bus, getJSON, openWS, sse, whileAuthOpen } from './api.js';

export const state = {
  snap: null,          // latest /v1/system/stream snapshot
  history: [],         // last 60 snapshots
  rows: [],            // inspector request rows, newest first
  events: [],          // governor events, newest first
  online: false,
};

export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) {
    if (k == null || k === false) continue;
    el.append(k.nodeType ? k : document.createTextNode(String(k)));
  }
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const num = (v, d = 0) => (typeof v === 'number' && isFinite(v) ? v.toFixed(d) : '—');
export function fmtTime(t) {
  const d = new Date((typeof t === 'number' ? t : Date.parse(t)) * (typeof t === 'number' ? 1000 : 1));
  return isNaN(d) ? '—' : d.toLocaleTimeString([], { hour12: false });
}

export function modelsList(json) {
  return Array.isArray(json) ? json : (json?.data || []);
}

const seenEvents = [];
function pushEvent(e) {
  const key = `${e.t}|${e.event}|${JSON.stringify(e.detail ?? e.data ?? '')}`;
  if (seenEvents.includes(key)) return;
  seenEvents.push(key);
  if (seenEvents.length > 300) seenEvents.shift();
  state.events.unshift(e);
  state.events.length = Math.min(state.events.length, 200);
  bus.dispatchEvent(new CustomEvent('event', { detail: e }));
}

function onSystemMsg(m) {
  if (!m || typeof m !== 'object') return;
  if (m.kind === 'event' || m.type === 'event' || (m.event && !m.host)) {
    pushEvent(m);
    return;
  }
  if (m.host || m.npu || m.server) {
    state.snap = m;
    state.history.push(m);
    if (state.history.length > 60) state.history.shift();
    bus.dispatchEvent(new CustomEvent('snapshot', { detail: m }));
    const gs = m.npu?.genai_state;
    bus.dispatchEvent(new CustomEvent('genai', { detail: { state: gs, owner: m.npu?.genai_owner } }));
  }
}

let sysWS = null;
function startSystem() {
  let ws;
  try { ws = openWS('/v1/system/stream'); } catch { setTimeout(startSystem, 3000); return; }
  sysWS = ws;
  ws.onmessage = (ev) => {
    if (typeof ev.data !== 'string') return;
    try { onSystemMsg(JSON.parse(ev.data)); } catch { /* ignore */ }
  };
  ws.onclose = () => { sysWS = null; setTimeout(async () => { await whileAuthOpen(); startSystem(); }, 3000); };
  ws.onerror = () => ws.close();
}

function onRow(r) {
  if (!r) return;
  if (r.kind === 'event') { pushEvent(r); return; }
  state.rows.unshift(r);
  state.rows.length = Math.min(state.rows.length, 500);
  bus.dispatchEvent(new CustomEvent('row', { detail: r }));
}

async function startInspector() {
  try {
    const j = await getJSON('/v1/inspector/recent');
    const list = Array.isArray(j) ? j : (j.data || j.rows || j.recent || []);
    for (const r of [...list].reverse()) {
      if (r.kind === 'event') state.events.push(r); else state.rows.push(r);
    }
    state.rows.reverse(); state.events.reverse();
    // ensure newest first regardless of server order
    state.rows.sort((a, b) => (b.t || 0) - (a.t || 0));
    state.events.sort((a, b) => (b.t || 0) - (a.t || 0));
    bus.dispatchEvent(new CustomEvent('row', { detail: null }));
  } catch { /* inspector unavailable; SSE keeps trying */ }
  sse('/v1/inspector', onRow);
}

async function healthLoop() {
  for (;;) {
    await whileAuthOpen();
    let ok = false;
    try { const j = await getJSON('/health'); ok = j.status === 'ok'; state.health = j; } catch { ok = false; }
    if (ok !== state.online) {
      state.online = ok;
      bus.dispatchEvent(new CustomEvent('online', { detail: ok }));
    }
    await new Promise((r) => setTimeout(r, 5000));
  }
}

export function startFeeds() {
  startSystem();
  startInspector();
  healthLoop();
}
export { bus };
