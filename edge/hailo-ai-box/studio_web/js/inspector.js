import { bus } from './api.js';
import { state, h, $, num, fmtTime } from './state.js';

let paused = false, cleared = 0;

function kb(n) { return n == null ? '' : n > 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`; }

function render() {
  if (paused) return;
  const fe = $('#f-endpoint').value.trim().toLowerCase();
  const fc = $('#f-client').value.trim().toLowerCase();
  const rows = state.rows.filter((r) => (r.t || 0) > cleared)
    .filter((r) => !fe || (r.endpoint || '').toLowerCase().includes(fe))
    .filter((r) => !fc || `${r.client || ''} ${r.model || ''}`.toLowerCase().includes(fc)).slice(0, 200);
  const body = $('#insp-table tbody');
  body.replaceChildren();
  if (!rows.length) { body.append(h('tr', {}, h('td', { colspan: 10, class: 'empty' }, 'No matching requests.'))); return; }
  for (const r of rows) {
    body.append(h('tr', { title: r.request_id || '' },
      h('td', {}, fmtTime(r.t)), h('td', { class: 'mono' }, r.endpoint || ''), h('td', {}, r.client || ''),
      h('td', {}, r.model || ''), h('td', {}, r.device || ''),
      h('td', { class: 'num' }, num(r.queue_ms)), h('td', { class: 'num' }, num(r.infer_ms)),
      h('td', { class: 'num' }, kb(r.bytes_in)), h('td', { class: 'num' }, kb(r.bytes_out)),
      h('td', {}, h('span', { class: `tag ${r.status >= 400 ? 'red' : 'green'}` }, r.status ?? ''))));
  }
}

export function init(root) {
  $('#f-endpoint').addEventListener('input', render);
  $('#f-client').addEventListener('input', render);
  $('#f-pause').addEventListener('click', (e) => {
    paused = !paused; e.target.textContent = paused ? 'Resume' : 'Pause'; render();
  });
  $('#f-clear').addEventListener('click', () => { cleared = Date.now() / 1000; render(); });
  bus.addEventListener('row', () => { if (!root.hidden) render(); });
}
export function show() { render(); }
