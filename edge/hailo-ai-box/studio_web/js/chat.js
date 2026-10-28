import { request, readSSE } from './api.js';
import { h, $, modelsList } from './state.js';
import { getJSON } from './api.js';

const KEY = 'aiboxChat';
let history = [];       // {role, content, image?, stat?}
let busy = false, ctl = null, attached = null, models = [];
let el = {};

function load() { try { history = JSON.parse(localStorage.getItem(KEY) || '[]'); } catch { history = []; } }
function save() {
  try { localStorage.setItem(KEY, JSON.stringify(history.slice(-40).map((m) => ({ ...m, image: undefined })))); } catch { /* full */ }
}

function render() {
  el.msgs.replaceChildren(...history.map((m) => h('div', { class: `msg ${m.role}` },
    m.image ? h('img', { src: m.image, alt: 'attached' }) : null, m.content,
    m.stat ? h('div', { class: 'stat' }, m.stat) : null)));
  if (!history.length) el.msgs.append(h('div', { class: 'empty' }, 'Ask the on-device model anything. Attach an image to use a vision-language model.'));
  el.msgs.scrollTop = el.msgs.scrollHeight;
}

function toDataURL(file) {
  return new Promise((res, rej) => {
    const img = new Image();
    const u = URL.createObjectURL(file);
    img.onload = () => {
      const k = Math.min(1, 800 / Math.max(img.width, img.height));
      const c = document.createElement('canvas');
      c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(u);
      res(c.toDataURL('image/jpeg', 0.85));
    };
    img.onerror = () => rej(new Error('not an image'));
    img.src = u;
  });
}

function apiMessages() {
  return history.filter((m) => !m.error).map((m) => {
    if (m.role === 'user' && m.imageData) {
      return { role: 'user', content: [{ type: 'text', text: m.content }, { type: 'image_url', image_url: { url: m.imageData } }] };
    }
    return { role: m.role, content: m.content };
  });
}

async function send() {
  const text = el.input.value.trim();
  if (busy || (!text && !attached)) return;
  const modelId = el.model.value;
  const isVlm = models.find((m) => m.id === modelId)?.modality === 'vlm';
  if (attached && !isVlm) {
    const v = models.find((m) => m.modality === 'vlm');
    if (v) el.model.value = v.id;
  }
  const user = { role: 'user', content: text || 'Describe this image.', image: attached, imageData: attached };
  history.push(user);
  attached = null; el.attachName.textContent = ''; el.input.value = '';
  const a = { role: 'assistant', content: '', stat: 'thinking…' };
  history.push(a);
  // only the latest image is sent; earlier ones are dropped to keep the VLM prompt small
  for (const m of history.slice(0, -2)) { delete m.imageData; }
  render();
  busy = true; el.send.textContent = 'Stop'; ctl = new AbortController();
  const t0 = performance.now();
  let first = null, n = 0;
  try {
    const hist = apiMessages();
    hist.pop(); // drop the empty assistant
    const res = await request('/v1/chat/completions', {
      method: 'POST', signal: ctl.signal, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: el.model.value, messages: hist, stream: true, max_tokens: 512 }),
    });
    await readSSE(res, (row, done) => {
      if (done || !row) return;
      const d = row.choices?.[0]?.delta?.content;
      if (d) { if (first == null) first = performance.now() - t0; n++; a.content += d; }
      if (row.tokens_per_s != null || row.ttft_ms != null) {
        a.stat = `TTFT ${Math.round(row.ttft_ms ?? first ?? 0)} ms · ${(row.tokens_per_s ?? 0).toFixed(1)} tok/s · ${row.meta?.model || el.model.value}`;
      }
      render();
    });
    if (a.stat === 'thinking…') {
      const dt = (performance.now() - t0) / 1000;
      a.stat = `TTFT ${Math.round(first ?? 0)} ms · ${(n / Math.max(dt, 0.001)).toFixed(1)} tok/s`;
    }
    if (!a.content) a.content = '(empty reply)';
  } catch (e) {
    if (e.name === 'AbortError') a.stat = 'stopped';
    else { if (!a.content) { a.content = `Error: ${e.message}`; a.error = true; } a.stat = 'failed'; }
  }
  busy = false; el.send.textContent = 'Send'; ctl = null;
  save(); render();
}

async function loadModels() {
  try { models = modelsList(await getJSON('/v1/models')).filter((m) => m.modality === 'chat' || m.modality === 'vlm'); } catch { models = []; }
  const cur = el.model.value;
  el.model.replaceChildren(...models.map((m) => h('option', { value: m.id }, `${m.id}${m.modality === 'vlm' ? ' (vision)' : ''}`)));
  if (cur) el.model.value = cur;
  else if (models.find((m) => m.id === 'qwen3-vl')) el.model.value = 'qwen3-vl';
  else if (models.find((m) => m.id === 'qwen2.5:1.5b')) el.model.value = 'qwen2.5:1.5b';
}

export function init(root) {
  load();
  root.append(
    h('h1', {}, 'Chat & VLM'),
    h('p', { class: 'lede' }, 'Private on-device language models. Switching between chat and vision models swaps the GenAI slot.'),
    h('div', { class: 'card chat-wrap' },
      h('div', { class: 'chat-bar' }, h('select', { id: 'c-model' }),
        h('button', { class: 'btn small ghost', id: 'c-clear' }, 'Clear history')),
      h('div', { class: 'msgs', id: 'c-msgs' }),
      h('div', { class: 'composer' },
        h('button', { class: 'btn icon-btn', id: 'c-attach', title: 'Attach image', 'aria-label': 'Attach image' }),
        h('input', { type: 'file', accept: 'image/*', id: 'c-file', hidden: true }),
        h('textarea', { id: 'c-input', rows: 1, placeholder: 'Message the box…' }),
        h('button', { class: 'btn primary', id: 'c-send' }, 'Send')),
      h('div', { class: 'muted small', id: 'c-attach-name' })));
  el = { msgs: $('#c-msgs'), input: $('#c-input'), send: $('#c-send'), model: $('#c-model'), attachName: $('#c-attach-name') };
  $('#c-attach').innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/></svg>';
  $('#c-send').addEventListener('click', () => (busy ? ctl?.abort() : send()));
  el.input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
  $('#c-attach').addEventListener('click', () => $('#c-file').click());
  $('#c-file').addEventListener('change', async (e) => {
    const f = e.target.files[0]; e.target.value = '';
    if (!f) return;
    try { attached = await toDataURL(f); el.attachName.textContent = `Attached: ${f.name}`; } catch (err) { el.attachName.textContent = `Cannot attach: ${err.message}`; }
    const v = models.find((m) => m.modality === 'vlm');
    if (v && models.find((m) => m.id === el.model.value)?.modality !== 'vlm') el.model.value = v.id;
  });
  $('#c-clear').addEventListener('click', () => { history = []; save(); render(); });
  loadModels();
  render();
}
export function show() { loadModels(); }
