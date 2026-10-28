import { bus, getJSON } from './api.js';
import { state, h, $, num, fmtTime, modelsList } from './state.js';

let models = [];
const SPARKS = [
  ['cpu', 'Pi CPU %', (s) => s.host?.cpu_pct, 100],
  ['temp', 'Pi temp °C', (s) => s.host?.temp_c, 90],
  ['npu', 'NPU util %', (s) => s.npu?.nnc_util_pct, 100],
  ['npuT', 'NPU temp °C', (s) => s.npu?.temp_c, 90],
  ['rps', 'Requests / s', (s) => s.server?.rps, null],
];

function gauge(id, label, color = '#58a6ff') {
  const r = 38, c = 2 * Math.PI * r;
  const ns = 'http://www.w3.org/2000/svg';
  const wrap = h('div', { class: 'gauge', id: `g-${id}` });
  wrap.innerHTML = `<svg viewBox="0 0 96 96"><circle cx="48" cy="48" r="${r}" fill="none" stroke="#0d1117" stroke-width="9"/>
    <circle class="arc" cx="48" cy="48" r="${r}" fill="none" stroke="${color}" stroke-width="9" stroke-linecap="round"
    stroke-dasharray="0 ${c}" transform="rotate(-90 48 48)"/></svg>`;
  void ns;
  wrap.append(h('div', { class: 'gv' }, '—'), h('div', { class: 'gk' }, label));
  wrap._c = c;
  return wrap;
}
function setGauge(id, text, pct, warn = 70, hot = 85) {
  const g = $(`#g-${id}`);
  if (!g) return;
  const p = Math.max(0, Math.min(100, pct || 0));
  const arc = g.querySelector('.arc');
  arc.setAttribute('stroke-dasharray', `${(p / 100) * g._c} ${g._c}`);
  arc.setAttribute('stroke', p >= hot ? '#f85149' : p >= warn ? '#d29922' : '#58a6ff');
  g.querySelector('.gv').textContent = text;
}

function drawSpark(canvas, vals, max) {
  const dpr = devicePixelRatio || 1;
  const w = canvas.clientWidth, hh = canvas.clientHeight;
  if (!w) return;
  canvas.width = w * dpr; canvas.height = hh * dpr;
  const g = canvas.getContext('2d');
  g.scale(dpr, dpr);
  g.clearRect(0, 0, w, hh);
  const v = vals.map((x) => (typeof x === 'number' ? x : 0));
  const top = max || Math.max(1, ...v) * 1.2;
  g.beginPath();
  v.forEach((y, i) => {
    const px = ((i + 60 - v.length) / 59) * w, py = hh - 2 - (Math.min(y, top) / top) * (hh - 4);
    i ? g.lineTo(px, py) : g.moveTo(px, py);
  });
  g.strokeStyle = '#58a6ff'; g.lineWidth = 1.6; g.stroke();
  g.lineTo(w, hh); g.lineTo(((60 - v.length) / 59) * w, hh); g.closePath();
  g.fillStyle = '#58a6ff22'; g.fill();
}

function onSnap(s) {
  const hst = s.host || {}, npu = s.npu || {};
  setGauge('cpu', `${num(hst.cpu_pct)}%`, hst.cpu_pct);
  setGauge('mem', `${num((hst.mem_used_mb || 0) / 1024, 1)} GB`, (hst.mem_used_mb / hst.mem_total_mb) * 100);
  setGauge('temp', `${num(hst.temp_c)}°C`, ((hst.temp_c || 0) / 90) * 100, 80, 90);
  setGauge('mhz', `${num(hst.arm_mhz)}`, ((hst.arm_mhz || 0) / 2400) * 100, 101, 101);
  $('#g-mhz .gk').textContent = `ARM MHz (load ${num(hst.load_1m, 1)})`;
  setGauge('npu', `${num(npu.nnc_util_pct)}%`, npu.nnc_util_pct);
  setGauge('npuram', `${num((npu.ram_used_mb || 0) / 1024, 1)} GB`, (npu.ram_used_mb / npu.ram_total_mb) * 100);
  setGauge('npuT', `${num(npu.temp_c)}°C`, ((npu.temp_c || 0) / 90) * 100, 80, 90);
  const cores = hst.cpu_per_core || [];
  const box = $('#cores');
  if (box.children.length !== cores.length) box.replaceChildren(...cores.map(() => h('div', {}, h('i'))));
  cores.forEach((c, i) => { box.children[i].firstChild.style.height = `${c}%`; box.children[i].title = `core ${i}: ${c}%`; });
  $('#g-genai .gv').textContent = npu.genai_owner || 'idle';
  $('#g-genai .gk').textContent = `GenAI slot · ${npu.genai_state || '—'}`;
  SPARKS.forEach(([id, , fn, max]) => {
    const c = $(`#sp-${id}`);
    drawSpark(c, state.history.map(fn), max);
    $(`#spv-${id}`).textContent = num(fn(s), 1);
  });
  renderResidency(s.models || []);
}

function renderResidency(stats) {
  const by = Object.fromEntries(stats.map((m) => [m.id, m]));
  const body = $('#residency tbody');
  body.replaceChildren();
  if (!models.length) { body.append(h('tr', {}, h('td', { colspan: 9, class: 'empty' }, 'No models reported.'))); return; }
  for (const m of models) {
    const s = by[m.id] || {};
    const st = m.state || (m.resident ? 'resident' : 'unloaded');
    const cls = st === 'resident' ? 'green' : st === 'failed' ? 'red' : st === 'unloaded' ? '' : 'amber';
    body.append(h('tr', {},
      h('td', { class: 'mono' }, m.id), h('td', {}, m.modality || ''), h('td', {}, m.runtime || ''),
      h('td', {}, h('span', { class: `tag ${cls}`, title: m.error || '' }, st), m.error ? h('span', { class: 'muted small' }, ` ${m.error}`) : null),
      h('td', { class: 'num' }, m.load_s ? `${num(m.load_s, 1)} s` : '—'),
      h('td', { class: 'num' }, num(s.fps, 1)), h('td', { class: 'num' }, num(s.p50_ms, 1)),
      h('td', { class: 'num' }, num(s.p95_ms, 1)), h('td', { class: 'num' }, s.requests ?? '—')));
  }
}

function renderEvents() {
  const log = $('#event-log');
  if (!state.events.length) { log.replaceChildren(h('div', { class: 'muted' }, 'No governor events yet.')); return; }
  log.replaceChildren(...state.events.slice(0, 60).map((e) => h('div', {},
    h('time', {}, fmtTime(e.t)), h('b', {}, e.event || e.type || 'event'), ' ',
    h('span', { class: 'muted' }, typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail ?? e.data ?? '')))));
}

async function refreshModels() {
  try { models = modelsList(await getJSON('/v1/models')); } catch { /* keep */ }
  renderResidency(state.snap?.models || []);
}

export function init(root) {
  const hg = $('#host-gauges'), ng = $('#npu-gauges');
  hg.append(gauge('cpu', 'CPU'), gauge('mem', 'Memory'), gauge('temp', 'Pi temp'), gauge('mhz', 'ARM MHz'));
  ng.append(gauge('npu', 'NNC util'), gauge('npuram', 'Device RAM'), gauge('npuT', 'NPU temp'), gauge('genai', 'GenAI', '#3fb950'));
  setGauge('genai', '', 0);
  const sp = $('#sparks');
  for (const [id, label] of SPARKS) {
    sp.append(h('div', { class: 'spark' }, h('div', { class: 'k' }, h('span', {}, label), h('b', { id: `spv-${id}` }, '—')),
      h('canvas', { id: `sp-${id}` })));
  }
  bus.addEventListener('snapshot', (e) => { if (!root.hidden) onSnap(e.detail); });
  bus.addEventListener('event', () => { if (!root.hidden) { renderEvents(); refreshModels(); } });
  bus.addEventListener('row', () => { if (!root.hidden) renderEvents(); });
}
export function show() {
  refreshModels();
  renderEvents();
  if (state.snap) onSnap(state.snap);
}
