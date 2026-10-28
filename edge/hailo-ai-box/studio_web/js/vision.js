import { bus, openWS, postForm, getJSON, apiBase, getKey } from './api.js';
import { h, $, num } from './state.js';

const TASKS = [
  ['detection', 'Object Detection'], ['pose', 'Pose'], ['segmentation', 'Segmentation'],
  ['lanes', 'Lane Detection'], ['dms', 'Driver Monitoring'], ['ocr', 'OCR'],
];
const SOURCES_BASE = [['c920e', 'C920e camera (box)'], ['f455', 'F455 camera (box)'],
  ['client', "This device's camera"], ['upload', 'Upload image']];
let SOURCES = SOURCES_BASE;
const SKELETON = [[0, 1], [0, 2], [1, 3], [2, 4], [5, 6], [5, 7], [7, 9], [6, 8], [8, 10], [5, 11], [6, 12],
  [11, 12], [11, 13], [13, 15], [12, 14], [14, 16]];
const COLORS = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#39c5cf', '#ff7b72', '#ffa657'];

const S = {
  ws: null, running: false, task: 'detection', source: 'c920e', fps: 10,
  img: null,            // current drawable (ImageBitmap / canvas / img)
  result: null, resultAt: 0, fpsMeas: 0, latency: null, degraded: null, frames: 0, t0: 0,
  videosOk: false, videos: [], videoT: null, videoDur: null, loading: null,
  stream: null, video: null, uploadShown: false, sendTimer: null, previewMeta: null, lastUpload: null, sizes: null,
};
let el = {};

// ---- RLE ----
function decodeRle(rle, w, hgt) {
  let counts = rle, W = w, H = hgt;
  if (rle && !Array.isArray(rle)) { counts = rle.counts; if (rle.size) [H, W] = rle.size; }
  if (!Array.isArray(counts)) return null;
  const c = document.createElement('canvas');
  c.width = W; c.height = H;
  const g = c.getContext('2d');
  const id = g.createImageData(W, H);
  let pos = 0, val = 0;
  for (const run of counts) {
    if (val) {
      for (let i = pos; i < pos + run && i < W * H; i++) {
        const x = Math.floor(i / H), y = i % H, o = (y * W + x) * 4;
        id.data[o] = 255; id.data[o + 1] = 255; id.data[o + 2] = 255; id.data[o + 3] = 255;
      }
    }
    pos += run; val ^= 1;
  }
  g.putImageData(id, 0, 0);
  return c;
}
const maskCache = new WeakMap();
function maskFor(d, col, rw, rh) {
  let c = maskCache.get(d);
  if (c === undefined) {
    const m = decodeRle(d.mask_rle, rw, rh);
    c = m ? tint(m, col) : null;
    maskCache.set(d, c);
  }
  return c;
}
function tint(mask, color) {
  const c = document.createElement('canvas');
  c.width = mask.width; c.height = mask.height;
  const g = c.getContext('2d');
  g.drawImage(mask, 0, 0);
  g.globalCompositeOperation = 'source-in';
  g.fillStyle = color; g.fillRect(0, 0, c.width, c.height);
  return c;
}

// ---- drawing ----
function draw() {
  const cv = el.canvas, g = cv.getContext('2d');
  const box = cv.parentElement.getBoundingClientRect();
  const dpr = devicePixelRatio || 1;
  if (cv.width !== Math.round(box.width * dpr)) { cv.width = Math.round(box.width * dpr); cv.height = Math.round(box.height * dpr); }
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.clearRect(0, 0, cv.width, cv.height);
  if (!S.img) return;
  const iw = S.img.width || S.img.videoWidth, ih = S.img.height || S.img.videoHeight;
  if (!iw) return;
  const k = Math.min(cv.width / iw, cv.height / ih);
  const ox = (cv.width - iw * k) / 2, oy = (cv.height - ih * k) / 2;
  g.drawImage(S.img, ox, oy, iw * k, ih * k);
  const r = S.result;
  if (!r) return;
  const rw = r.image_width || iw, rh = r.image_height || ih;
  const sx = (iw * k) / rw, sy = (ih * k) / rh;
  const X = (x) => ox + x * sx, Y = (y) => oy + y * sy;
  g.lineWidth = Math.max(2, 2 * dpr);
  g.font = `${Math.round(12 * dpr)}px Inter, sans-serif`;
  g.textBaseline = 'top';
  let dets = r.detections || [];
  const dmsView = !!r.dms;
  if (dmsView) dets = dets.slice(0, 1);
  dets.forEach((d, i) => {
    const col = COLORS[i % COLORS.length];
    if (d.mask_rle) {
      const m = maskFor(d, col, rw, rh);
      if (m) { g.globalAlpha = 0.4; g.drawImage(m, ox, oy, iw * k, ih * k); g.globalAlpha = 1; }
    }
    if (d.bbox) {
      const [x1, y1, x2, y2] = d.bbox;
      g.strokeStyle = col; g.strokeRect(X(x1), Y(y1), (x2 - x1) * sx, (y2 - y1) * sy);
      const label = dmsView ? '' : `${d.class ?? ''} ${d.confidence != null ? Math.round(d.confidence * 100) + '%' : ''}`.trim();
      if (label) {
        const tw = g.measureText(label).width + 8 * dpr, th = 16 * dpr;
        g.fillStyle = col; g.fillRect(X(x1), Math.max(0, Y(y1) - th), tw, th);
        g.fillStyle = '#0f1216'; g.fillText(label, X(x1) + 4 * dpr, Math.max(0, Y(y1) - th) + 2 * dpr);
      }
    }
    const kp = dmsView ? null : d.keypoints;
    if (kp && kp.length) {
      g.strokeStyle = col;
      for (const [a, b] of SKELETON) {
        const p = kp[a], q = kp[b];
        if (p && q && (p[2] ?? 1) > 0.3 && (q[2] ?? 1) > 0.3) {
          g.beginPath(); g.moveTo(X(p[0]), Y(p[1])); g.lineTo(X(q[0]), Y(q[1])); g.stroke();
        }
      }
      g.fillStyle = '#fff';
      for (const p of kp) {
        if ((p[2] ?? 1) > 0.3) { g.beginPath(); g.arc(X(p[0]), Y(p[1]), 3 * dpr, 0, 7); g.fill(); }
      }
    }
  });
  if (r.lanes) {
    g.strokeStyle = '#3fb950'; g.lineWidth = 4 * dpr;
    for (const lane of r.lanes) {
      g.beginPath();
      lane.forEach(([x, y], i) => (i ? g.lineTo(X(x), Y(y)) : g.moveTo(X(x), Y(y))));
      g.stroke();
    }
  }
  if (r.text) {
    for (const t of r.text) {
      if (!t.bbox) continue;
      const [x1, y1, x2, y2] = t.bbox;
      g.strokeStyle = '#d29922'; g.lineWidth = 2 * dpr;
      g.strokeRect(X(x1), Y(y1), (x2 - x1) * sx, (y2 - y1) * sy);
      const tw = g.measureText(t.text).width + 8 * dpr;
      g.fillStyle = '#d29922'; g.fillRect(X(x1), Y(y2), tw, 16 * dpr);
      g.fillStyle = '#0f1216'; g.fillText(t.text, X(x1) + 4 * dpr, Y(y2) + 2 * dpr);
    }
  }
}

const DMS_CHIP = { attentive: 'green', looking_away: 'amber', eyes_not_visible: 'amber', phone_in_hand: 'red', no_driver: '' };
function renderDms() {
  const d = S.task === 'dms' && S.running ? S.result?.dms : null;
  el.dms.hidden = !d;
  if (!d) return;
  const st = d.status || 'no_driver';
  el.dmsChip.className = `tag ${DMS_CHIP[st] ?? ''}`;
  el.dmsChip.textContent = st.replace(/_/g, ' ');
  el.dmsSummary.textContent = d.summary || st.replace(/_/g, ' ');
  const f = (v) => (v == null ? '—' : typeof v === 'boolean' ? (v ? 'yes' : 'no') : String(v));
  el.dmsRaw.textContent = `yaw ${d.head_yaw_deg != null ? num(d.head_yaw_deg, 0) + '°' : '—'} · eyes_visible ${f(d.eyes_visible)} · eyes_on_road ${f(d.eyes_on_road)} · phone ${f(d.phone_in_hand)}`;
}

function setDiag() {
  const r = S.result;
  const vals = {
    task: TASKS.find((t) => t[0] === S.task)[1],
    source: (SOURCES.find((t) => t[0] === S.source) || ['', S.source])[1],
    res: r?.image_width ? `${r.image_width}×${r.image_height}` : (S.img ? `${S.img.width || S.img.videoWidth}×${S.img.height || S.img.videoHeight}` : '—'),
    engine: 'Hailo-10H', fps: S.running ? num(S.fpsMeas, 1) : '—',
    lat: r?.inference_ms != null ? `${num(r.inference_ms, 1)} ms` : '—',
    n: r ? (r.detections?.length ?? r.text?.length ?? r.lanes?.length ?? (r.dms ? r.dms.faces : 0)) : '—',
    deg: S.loading ? `Loading ${S.loading}…` : (S.degraded ? S.degraded : 'none'),
    vt: S.source.startsWith('video:') && S.videoT != null ? `${num(S.videoT, 1)} s${S.videoDur ? ' / ' + num(S.videoDur, 1) + ' s' : ''}` : '—',
  };
  for (const [k, v] of Object.entries(vals)) el[`d_${k}`].textContent = v;
  el.d_deg.style.color = (S.degraded || S.loading) ? '#d29922' : '';
  el.d_vt.parentElement.hidden = !S.source.startsWith('video:');
  renderDms();
}

// ---- running ----
function setStatus(t, cls = '') { el.status.textContent = t; el.status.className = `tag ${cls}`; }

function onResult(msg) {
  const r = msg.results || msg;
  S.result = r;
  S.loading = null;
  if (msg.video_t != null) S.videoT = msg.video_t; else if (r.video_t != null) S.videoT = r.video_t;
  S.degraded = msg.degraded || r.degraded || null;
  S.frames++;
  const now = performance.now();
  S.fpsMeas = msg.fps != null ? msg.fps : (S.frames * 1000) / Math.max(1, now - S.t0);
  setDiag();
}

async function setPreview(buf) {
  try {
    if (S.previewMeta?.format === 'rgb565') return;
    const bmp = await createImageBitmap(new Blob([buf], { type: 'image/jpeg' }));
    S.img?.close?.();
    S.img = bmp;
  } catch { /* bad frame */ }
}

// Stop and task/source changes leave no stale frame or overlay: drop the image and result, show the placeholder.
function syncStage() { el.placeholder.hidden = S.running || S.uploadShown; }
function clearView() {
  try { S.img?.close?.(); } catch { /* not a bitmap */ }
  S.img = null; S.result = null; S.uploadShown = false; S.previewMeta = null;
  const cv = el.canvas;
  cv.getContext('2d').clearRect(0, 0, cv.width, cv.height);
  syncStage();
}

function stop() {
  S.running = false;
  clearInterval(S.sendTimer); S.sendTimer = null;
  if (S.ws) { try { S.ws.close(); } catch { /* */ } S.ws = null; }
  if (S.stream) { S.stream.getTracks().forEach((t) => t.stop()); S.stream = null; }
  if (S.video) { S.video.srcObject = null; S.video = null; }
  clearView();
  el.start.textContent = 'Start';
  setStatus('Stopped');
  setDiag();
}

function showError(msg) { el.hint.hidden = false; el.hint.textContent = msg; el.hint.classList.add('err'); }

async function start() {
  if (S.running) { stop(); return; }
  S.result = null; S.frames = 0; S.t0 = performance.now(); S.degraded = null; S.loading = null; S.videoT = null; el.hint.hidden = true; el.hint.classList.remove('err');
  if (S.source === 'upload') { el.file.click(); return; }
  if (S.source === 'client') {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      showError(`Camera access needs HTTPS. Open https://${location.hostname}:8443/ and accept the certificate.`);
      return;
    }
    try {
      S.stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 }, audio: false });
    } catch (e) { showError(`Camera unavailable: ${e.message}`); return; }
    S.video = h('video', { autoplay: true, muted: true, playsinline: true });
    S.video.srcObject = S.stream;
    await S.video.play().catch(() => {});
    S.img = S.video;
  }
  const ws = openWS('/v1/vision/stream');
  S.ws = ws; S.running = true; S.previewMeta = null;
  syncStage();
  el.start.textContent = 'Stop';
  setStatus('Connecting…');
  S.videoDur = S.videos.find((v) => `video:${v.id}` === S.source)?.duration_s ?? null;
  ws.onopen = () => {
    if (S.ws !== ws) return;
    setStatus('Live', 'green');
    const first = { source: S.source, task: S.task, fps: S.fps };
    if (S.source.startsWith('video:')) { first.source = 'video'; first.video_id = S.source.slice(6); }
    if (S.source !== 'client') { first.preview = 'jpeg'; first.preview_width = 640; }
    ws.send(JSON.stringify(first));
    if (S.source === 'client') {
      const cv = document.createElement('canvas');
      let busy = false;
      S.sendTimer = setInterval(() => {
        const v = S.video;
        if (!v || !v.videoWidth || busy || ws.bufferedAmount > 400000) return;
        busy = true;
        cv.width = 640; cv.height = Math.round((640 * v.videoHeight) / v.videoWidth);
        cv.getContext('2d').drawImage(v, 0, 0, cv.width, cv.height);
        cv.toBlob((b) => { if (b && ws.readyState === 1) ws.send(b); busy = false; }, 'image/jpeg', 0.7);
      }, 1000 / S.fps);
    }
  };
  ws.onmessage = (ev) => {
    if (S.ws !== ws) return;
    if (typeof ev.data === 'string') {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type === 'preview') { S.previewMeta = m; return; }
      if (m.type === 'status') {
        if (m.state === 'loading') { S.loading = m.model || 'model'; S.result = null; setDiag(); } else { S.loading = null; setDiag(); }
        return;
      }
      if (m.error || m.type === 'error') {
        const t = m.message || m.error?.message || (typeof m.error === 'string' ? m.error : '') || 'stream error';
        if (!el.hint.classList.contains('err') || el.hint.textContent !== t) showError(t);
        return;
      }
      onResult(m);
    } else {
      setPreview(ev.data);
    }
  };
  ws.onclose = () => { if (S.ws === ws && S.running) { stop(); setStatus('Disconnected', 'red'); } };
  ws.onerror = () => S.ws === ws && showError('Vision stream failed. Is the camera free and the server reachable?');
}

async function runUpload(file) {
  stop();
  setStatus('Running…', 'blue');
  try {
    const bmp = await createImageBitmap(file);
    S.img = bmp; S.lastUpload = file; S.uploadShown = true;
    syncStage();
    const form = new FormData();
    form.append('image', file); form.append('task', S.task);
    const t = performance.now();
    const res = await postForm('/v1/vision/detect', form);
    S.result = res; S.degraded = null; S.frames = 1;
    S.fpsMeas = 1000 / (performance.now() - t);
    setStatus('Done', 'green');
  } catch (e) { setStatus('Error', 'red'); showError(`Detection failed: ${e.message}`); }
  setDiag();
}

async function frameBlob() {
  const cv = document.createElement('canvas');
  const src = S.img;
  if (!src) return null;
  const w = src.width || src.videoWidth, hh = src.height || src.videoHeight;
  cv.width = Math.min(w, 960); cv.height = Math.round((cv.width * hh) / w);
  cv.getContext('2d').drawImage(src, 0, 0, cv.width, cv.height);
  return new Promise((r) => cv.toBlob(r, 'image/jpeg', 0.85));
}

async function ask() {
  const q = el.askText.value.trim() || 'Describe this scene.';
  const blob = await frameBlob();
  if (!blob) { el.answer.textContent = 'Start a source or upload an image first.'; return; }
  el.askBtn.disabled = true; el.answer.textContent = 'Asking the VLM (the GenAI slot may need to swap)…';
  const t = performance.now();
  try {
    const form = new FormData();
    form.append('image', blob, 'frame.jpg'); form.append('prompt', q); form.append('model', el.askModel.value);
    const j = await postForm('/v1/vision/ask', form);
    el.answer.textContent = `${j.answer ?? ''}\n\n${((performance.now() - t) / 1000).toFixed(1)} s · ${j.meta?.model || el.askModel.value}`;
  } catch (e) { el.answer.textContent = `Failed: ${e.message}`; }
  el.askBtn.disabled = false;
}

function rebuildSources(select) {
  const sel = $('#v-source');
  const opts = [...SOURCES_BASE];
  if (S.videosOk) {
    for (const v of S.videos) opts.push([`video:${v.id}`, `Video: ${v.name || v.id}`]);
    opts.push(['__upload_video', 'Upload video…']);
  }
  SOURCES = opts.filter((o) => o[0] !== '__upload_video');
  sel.replaceChildren(...opts.map(([v, l]) => h('option', { value: v }, l)));
  sel.value = select || S.source;
}

async function loadVideos() {
  try {
    const j = await getJSON('/v1/vision/videos');
    S.videos = Array.isArray(j?.data) ? j.data : [];
    S.videosOk = true;
  } catch { S.videos = []; S.videosOk = false; }
  rebuildSources();
  if (S.task === 'lanes' && S.videos.length && !S.source.startsWith('video:')) rebuildSources(S.source = `video:${S.videos[0].id}`);
  setDiag();
}

function uploadVideo(file) {
  stop();
  el.upl.hidden = false; el.upl.textContent = 'Uploading 0%';
  const xhr = new XMLHttpRequest();
  xhr.open('POST', apiBase + '/v1/vision/videos');
  const k = getKey();
  if (k) xhr.setRequestHeader('Authorization', `Bearer ${k}`);
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) el.upl.textContent = `Uploading ${Math.round((100 * e.loaded) / e.total)}%`; };
  xhr.onerror = () => { el.upl.textContent = ''; showError('Video upload failed: network error'); };
  xhr.onload = () => {
    let j = null; try { j = JSON.parse(xhr.responseText); } catch { /* */ }
    if (xhr.status >= 200 && xhr.status < 300 && j?.id) {
      el.upl.hidden = true;
      S.videos = [...S.videos.filter((v) => v.id !== j.id), j];
      S.source = `video:${j.id}`; S.videosOk = true;
      rebuildSources(S.source); setDiag();
    } else {
      el.upl.textContent = '';
      showError(`Video upload failed: ${j?.error?.message || j?.detail || xhr.status}`);
    }
  };
  const form = new FormData(); form.append('file', file);
  xhr.send(form);
}

let raf = 0;
function loop() { if (!el.canvas.closest('section').hidden) draw(); raf = requestAnimationFrame(loop); }

export function init(root) {
  const sel = (id, opts, cur) => h('select', { id }, opts.map(([v, l]) => h('option', { value: v, ...(v === cur ? { selected: true } : {}) }, l)));
  const diagRow = (k, id) => h('div', {}, h('span', {}, k), h('span', { id: `d_${id}` }, '—'));
  root.append(
    h('h1', {}, 'Computer Vision'),
    h('p', { class: 'lede' }, 'Run perception tasks on the Hailo-10H with live overlays and inference diagnostics.'),
    h('div', { class: 'vgrid' },
      h('div', { class: 'card' },
        h('div', { class: 'card-head' }, h('h3', {}, 'Perception View'), h('span', { class: 'tag', id: 'v-status' }, 'Stopped')),
        h('div', { class: 'stage' }, h('canvas', { id: 'v-canvas' }), h('div', { class: 'placeholder', id: 'v-placeholder' }, 'Stopped — press Start'), h('div', { class: 'hint', id: 'v-hint', hidden: true })),
        h('div', { class: 'dms-panel', id: 'v-dms', hidden: true },
          h('div', { class: 'dms-head' }, h('span', { class: 'tag', id: 'v-dms-chip' }, '—')),
          h('div', { class: 'dms-summary', id: 'v-dms-summary' }, ''),
          h('div', { class: 'dms-raw muted small', id: 'v-dms-raw' }, '')),
        h('div', { class: 'controls' },
          sel('v-task', TASKS, S.task), sel('v-source', SOURCES, S.source),
          h('span', { class: 'muted small', id: 'v-upl', hidden: true }),
          h('label', { class: 'muted small' }, 'fps ', h('input', { id: 'v-fps', type: 'number', min: 1, max: 30, value: S.fps, style: 'width:64px' })),
          h('button', { class: 'btn primary', id: 'v-start' }, 'Start'),
          h('input', { id: 'v-file', type: 'file', accept: 'image/*', hidden: true })),
        h('div', { class: 'ask' },
          h('h3', {}, 'Ask the VLM about this frame'),
          h('div', { class: 'controls', style: 'margin-top:0' },
            h('input', { id: 'v-ask-text', placeholder: 'What is in front of the car?', style: 'flex:1 1 220px' }),
            sel('v-ask-model', [['qwen3-vl', 'Qwen3-VL'], ['qwen2-vl', 'Qwen2-VL']], 'qwen3-vl'),
            h('button', { class: 'btn', id: 'v-ask-btn' }, 'Ask')),
          h('div', { class: 'answer', id: 'v-answer', hidden: true }))),
      h('div', { class: 'card' }, h('h3', {}, 'Inference Diagnostics'),
        h('div', { class: 'diag' },
          diagRow('Vision Task', 'task'), diagRow('Input Source', 'source'), diagRow('Resolution', 'res'), diagRow('Video Time', 'vt'),
          diagRow('Inference Engine', 'engine'), diagRow('FPS', 'fps'), diagRow('Inference Latency', 'lat'),
          diagRow('Detections', 'n'), diagRow('Degraded', 'deg')),
        h('p', { class: 'muted small' }, 'While a GenAI model generates, vision slows down. The reason shows under Degraded.'))));
  el = {
    canvas: $('#v-canvas'), placeholder: $('#v-placeholder'), hint: $('#v-hint'), status: $('#v-status'), start: $('#v-start'), file: $('#v-file'),
    dms: $('#v-dms'), dmsChip: $('#v-dms-chip'), dmsSummary: $('#v-dms-summary'), dmsRaw: $('#v-dms-raw'),
    vfile: null, upl: $('#v-upl'),
    askText: $('#v-ask-text'), askBtn: $('#v-ask-btn'), askModel: $('#v-ask-model'), answer: $('#v-answer'),
  };
  for (const id of ['task', 'n', 'source', 'vt', 'res', 'engine', 'fps', 'lat', 'deg']) el[`d_${id}`] = $(`#d_${id}`);
  el.d_n = $('#d_n');
  el.vfile = h('input', { type: 'file', accept: 'video/mp4,video/webm,video/quicktime,video/x-matroska,.mkv,.mov', hidden: true });
  root.append(el.vfile);
  const srcSel = $('#v-source');
  // Close and reopen the stream on any task/source change so the server never sees a mid-stream switch.
  const restart = () => { const was = S.running; const s0 = S.source; stop(); S.source = s0; el.hint.hidden = true; if (was) start(); setDiag(); };
  $('#v-task').addEventListener('change', (e) => {
    S.task = e.target.value; S.result = null;
    if (S.task === 'lanes' && S.videos.length && !S.source.startsWith('video:')) {
      S.source = `video:${S.videos[0].id}`; srcSel.value = S.source;
    }
    if (S.running) restart(); else { clearView(); if (S.lastUpload && S.source === 'upload') runUpload(S.lastUpload); }
    setDiag();
  });
  srcSel.addEventListener('change', (e) => {
    if (e.target.value === '__upload_video') { srcSel.value = S.source; el.vfile.click(); return; }
    const was = S.running;
    stop(); S.source = e.target.value; clearView(); el.hint.hidden = true; S.videoDur = null; S.videoT = null;
    if (was && S.source !== 'upload') start();
    setDiag();
  });
  el.vfile.addEventListener('change', () => { const f = el.vfile.files[0]; el.vfile.value = ''; if (f) uploadVideo(f); });
  $('#v-fps').addEventListener('change', (e) => { S.fps = Math.max(1, Math.min(30, Number(e.target.value) || 10)); });
  el.start.addEventListener('click', start);
  el.file.addEventListener('change', () => { const f = el.file.files[0]; if (f) { el.hint.hidden = true; runUpload(f); } el.file.value = ''; });
  el.askBtn.addEventListener('click', async () => { el.answer.hidden = false; await ask(); });
  syncStage();
  setDiag();
  loadVideos();
  loop();
  void bus; void raf;
}
export function hide() { stop(); }
export function show() { setDiag(); }
