import { bus, getJSON, apiBase } from './api.js';
import { state, h, $, num, fmtTime, modelsList } from './state.js';

const GROUPS = [
  { key: 'voice', title: 'Voice Intelligence', sub: 'ASR · Conversational AI · TTS', path: '/v1/voice/stream',
    mods: ['asr', 'tts'], icon: '◎', page: 'voice' },
  { key: 'vision', title: 'Computer Vision', sub: 'Detection · Pose · Segmentation · Lanes · DMS · OCR',
    path: '/v1/vision/detect', mods: ['vision'], icon: '▧', page: 'vision' },
  { key: 'chat', title: 'Language Models', sub: 'Local chat · Streaming responses',
    path: '/v1/chat/completions', mods: ['chat'], icon: '▤', page: 'chat' },
  { key: 'vlm', title: 'Vision-Language', sub: 'Ask questions about an image',
    path: '/v1/vision/ask', mods: ['vlm'], icon: '✦', page: 'chat' },
];
const ENDPOINTS = 15;

let models = [];

function chip(m) {
  const st = m.state || (m.resident ? 'resident' : 'unloaded');
  return h('span', { class: `chip ${st}`, title: m.error || st }, h('span', { class: 'dot' }), m.id);
}

function renderRegistry() {
  const root = $('#registry');
  root.replaceChildren();
  for (const g of GROUPS) {
    const ms = models.filter((m) => g.mods.includes(m.modality));
    root.append(h('a', { class: 'svc', href: `#${g.page}` },
      h('div', { class: 'svc-icon' }, g.icon),
      h('div', { class: 'svc-body' },
        h('div', { class: 'svc-title' }, g.title),
        h('div', { class: 'svc-sub' }, g.sub),
        h('div', { class: 'svc-path' }, g.path),
        h('div', { class: 'chips' }, ms.length ? ms.map(chip) : h('span', { class: 'muted small' }, 'no models reported'))),
      h('div', { class: 'svc-go' }, '›')));
  }
  $('#cnt-services').textContent = models.length || '—';
}

function tile(id, k) {
  return h('div', { class: 'tile', id: `tile-${id}` }, h('div', { class: 'k' }, k),
    h('div', { class: 'v' }, '—'), h('div', { class: 'bar' }, h('i', { style: 'width:0%' })));
}
function setTile(id, text, pct, warn = 70, hot = 85) {
  const t = $(`#tile-${id}`);
  if (!t) return;
  t.querySelector('.v').textContent = text;
  const i = t.querySelector('i');
  const p = Math.max(0, Math.min(100, pct || 0));
  i.style.width = `${p}%`;
  i.className = p >= hot ? 'hot' : p >= warn ? 'warn' : '';
}

function onSnap(s) {
  const hst = s.host || {}, npu = s.npu || {}, srv = s.server || {};
  setTile('cpu', `${num(hst.cpu_pct)}%`, hst.cpu_pct);
  setTile('mem', `${num((hst.mem_used_mb || 0) / 1024, 1)} GB`, (hst.mem_used_mb / hst.mem_total_mb) * 100);
  setTile('temp', `${num(hst.temp_c)}°C`, ((hst.temp_c || 0) / 90) * 100, 80, 90);
  setTile('npu', `${num(npu.nnc_util_pct)}%`, npu.nnc_util_pct);
  setTile('npuram', `${num((npu.ram_used_mb || 0) / 1024, 1)} GB`, (npu.ram_used_mb / npu.ram_total_mb) * 100);
  setTile('npuT', `${num(npu.temp_c)}°C`, ((npu.temp_c || 0) / 90) * 100, 80, 90);
  const owner = npu.genai_owner || 'idle';
  const t = $('#tile-genai');
  t.querySelector('.v').textContent = owner;
  t.querySelector('.v').style.fontSize = '15px';
  t.querySelector('.k').textContent = `GenAI slot · ${npu.genai_state || '—'}`;
  t.querySelector('i').style.width = npu.genai_state === 'swapping' ? '100%' : npu.genai_owner ? '100%' : '0%';
  $('#cnt-rps').textContent = srv.rps != null ? num(srv.rps, 1) : '—';
  if (srv.clients != null) $('#cnt-clients').textContent = srv.clients;
}

function renderStrip() {
  const root = $('#strip');
  const rows = state.rows.slice(0, 8);
  if (!rows.length) { root.replaceChildren(h('div', { class: 'empty' }, 'No requests yet.')); return; }
  const body = h('tbody');
  for (const r of rows) {
    body.append(h('tr', {},
      h('td', {}, fmtTime(r.t)), h('td', { class: 'mono' }, r.endpoint || ''), h('td', {}, r.model || ''),
      h('td', {}, r.device || ''), h('td', { class: 'num' }, num(r.infer_ms)),
      h('td', {}, h('span', { class: `tag ${r.status >= 400 ? 'red' : 'green'}` }, r.status ?? ''))));
  }
  root.replaceChildren(h('table', { class: 'tbl' }, body));
}

async function refreshModels() {
  try { models = modelsList(await getJSON('/v1/models')); } catch { /* keep old */ }
  renderRegistry();
}
async function refreshClients() {
  try {
    const j = await getJSON('/v1/clients');
    const n = Array.isArray(j) ? j.length : (j.data || j.clients || []).length;
    if (state.snap?.server?.clients == null) $('#cnt-clients').textContent = n;
  } catch { /* ignore */ }
}

export function init() {
  $('#cnt-endpoints').textContent = ENDPOINTS;
  $('#base-url').textContent = apiBase;
  $('#auth-note').textContent = 'Send the key as "Authorization: Bearer <key>" or "?key=" for WebSocket/SSE. Loopback clients need no key.';
  $('#copy-url').addEventListener('click', async () => {
    const b = $('#copy-url');
    try { await navigator.clipboard.writeText(apiBase); b.textContent = 'Copied'; }
    catch {
      const r = document.createRange(); r.selectNodeContents($('#base-url'));
      getSelection().removeAllRanges(); getSelection().addRange(r); b.textContent = 'Selected';
    }
    setTimeout(() => { b.textContent = 'Copy'; }, 1500);
  });
  const tiles = $('#tiles');
  [['cpu', 'CPU usage'], ['mem', 'Memory used'], ['temp', 'Pi temperature'], ['npu', 'NPU utilisation'],
    ['npuram', 'NPU RAM'], ['npuT', 'NPU temperature'], ['genai', 'GenAI slot']].forEach(([i, k]) => tiles.append(tile(i, k)));
  bus.addEventListener('snapshot', (e) => onSnap(e.detail));
  bus.addEventListener('row', renderStrip);
  bus.addEventListener('online', refreshModels);
  bus.addEventListener('event', refreshModels);
  if (state.snap) onSnap(state.snap);
  renderStrip();
  renderRegistry();
  refreshModels();
  refreshClients();
  setInterval(() => { if (!$('#page-overview').hidden) { refreshModels(); refreshClients(); } }, 5000);
}
