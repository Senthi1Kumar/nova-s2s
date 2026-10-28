import { getJSON, postJSON, request, openWS } from './api.js';

let ws = null;
let previewUrl = null;
let previewFrame = null;
let previewFace = null;
let previewImageSize = null;
let authFlashUntil = 0;
let root = null;
let busy = false;

function clearPreview(el, note = 'Stopped — press Start') {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  previewFrame = null;
  previewFace = null;
  previewImageSize = null;
  const canvas = el.querySelector('#face-preview');
  if (canvas) canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
  const ph = el.querySelector('#face-preview-ph');
  if (ph) { ph.hidden = false; ph.textContent = note; }
  const state = el.querySelector('#face-preview-state');
  if (state) { state.textContent = 'Stopped'; state.className = 'tag'; }
  const btn = el.querySelector('#face-preview-toggle');
  if (btn) btn.textContent = 'Start';
}

function stopPreview(el) {
  const old = ws;
  ws = null;
  if (old) { old.onclose = null; try { old.close(); } catch { /* closed */ } }
  clearPreview(el);
}

function drawOval(ctx, face, point, color, dpr) {
  if (!face?.bbox) return;
  const [x1, y1, x2, y2] = face.bbox;
  const w = x2 - x1, h = y2 - y1;
  if (w <= 0 || h <= 0) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = 2 * dpr;
  ctx.beginPath();
  for (let i = 0; i <= 32; i++) {
    const a = Math.PI * 2 * i / 32;
    const p = point([x1 + w * (0.5 + 0.49 * Math.cos(a)), y1 + h * (0.5 + 0.49 * Math.sin(a))]);
    if (i === 0) ctx.moveTo(p[0], p[1]); else ctx.lineTo(p[0], p[1]);
  }
  ctx.stroke();
}

function drawPreview(el) {
  const canvas = el.querySelector('#face-preview');
  if (!canvas || !previewFrame?.complete || !previewFrame.naturalWidth) return;
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, canvas.clientWidth);
  const scaleFit = Math.min(width / previewFrame.naturalWidth, 460 / previewFrame.naturalHeight);
  const w = Math.round(previewFrame.naturalWidth * scaleFit);
  const h = Math.round(previewFrame.naturalHeight * scaleFit);
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(h * dpr);
  canvas.style.height = `${h}px`;
  const ctx = canvas.getContext('2d');
  const ox = (width - w) / 2;
  ctx.drawImage(previewFrame, ox * dpr, 0, w * dpr, h * dpr);
  if (!previewFace || !previewImageSize?.w || !previewImageSize?.h) return;
  const ix = previewFrame.naturalWidth / previewImageSize.w;
  const iy = previewFrame.naturalHeight / previewImageSize.h;
  const point = ([x, y]) => [(ox + x * ix * scaleFit) * dpr, y * iy * scaleFit * dpr];
  const color = performance.now() < authFlashUntil ? '#3fb950' : '#f0f6fc';
  drawOval(ctx, previewFace, point, color, dpr);
}

function startPreview(el) {
  if (ws) return;
  const state = el.querySelector('#face-preview-state');
  const socket = openWS('/v1/vision/stream');
  ws = socket;
  el.querySelector('#face-preview-ph').hidden = true;
  el.querySelector('#face-preview-toggle').textContent = 'Stop';
  state.textContent = 'Connecting…';
  socket.onopen = () => {
    if (ws !== socket) return;
    socket.send(JSON.stringify({ source: 'f455', task: 'face_track', fps: 10, preview: 'jpeg', preview_width: 480 }));
  };
  socket.onmessage = (ev) => {
    if (ws !== socket) return;
    if (ev.data instanceof ArrayBuffer) {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = URL.createObjectURL(new Blob([ev.data], { type: 'image/jpeg' }));
      const frame = new Image();
      previewFrame = frame;
      frame.onload = () => { if (previewFrame === frame) drawPreview(el); };
      frame.src = previewUrl;
      return;
    }
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'result') {
      const results = msg.results || {};
      previewFace = results.face || results.detections?.[0] || null;
      previewImageSize = { w: results.image_width || 0, h: results.image_height || 0 };
      state.textContent = previewFace ? 'Tracking face · F455 authentication' : 'No face detected';
      drawPreview(el);
    } else if (msg.type === 'status' && msg.state === 'loading') {
      state.textContent = `Loading ${msg.model || 'face tracker'}…`;
    } else if (msg.type === 'error') {
      state.textContent = msg.error?.message || 'Preview error';
    }
  };
  socket.onclose = () => {
    if (ws !== socket) return;
    ws = null;
    clearPreview(el, 'Stopped — press Start');
  };
}

function setStatus(el, text, color = '#8b949e') {
  const status = el.querySelector('#face-auth-status');
  status.textContent = text;
  status.style.color = color;
}

function setBusy(el, on) {
  busy = on;
  for (const id of ['#face-auth', '#face-enroll-submit', '#face-delete-all']) {
    const button = el.querySelector(id);
    if (button) button.disabled = on;
  }
}

function showResult(el, data) {
  const result = el.querySelector('#face-result');
  result.textContent = JSON.stringify(data, null, 2);
  if (data.algo_flow) el.querySelector('#face-mode').textContent =
    `Mode: ${data.algo_flow} · Security: ${data.security_level} · Faces: ${data.face_selection_policy}`;
  if (Array.isArray(data.faces) && data.faces.length && previewFrame?.naturalWidth) {
    previewFace = { bbox: [data.faces[0].x, data.faces[0].y,
      data.faces[0].x + data.faces[0].w, data.faces[0].y + data.faces[0].h] };
    previewImageSize = { w: data.frame_width || 1080, h: data.frame_height || 1920 };
  }
  if (data.authenticated !== undefined) {
    setStatus(el, data.message || (data.authenticated ? 'Authenticated' : `Not authenticated — ${data.status}`),
      data.authenticated ? '#3fb950' : '#f85149');
    if (data.authenticated) {
      authFlashUntil = performance.now() + 6000;
      drawPreview(el);
    }
  } else if (data.enrolled !== undefined || data.deleted !== undefined || data.message) {
    const ok = data.enrolled === true || data.deleted === true;
    setStatus(el, data.message || (ok ? 'Success' : 'Face ID operation failed'), ok ? '#3fb950' : '#f85149');
  }
}

function userRow(el, user) {
  const row = document.createElement('div');
  row.className = 'face-user-row';
  const identity = document.createElement('div');
  identity.className = 'face-user-identity';
  const name = document.createElement('strong');
  name.textContent = user.name;
  const meta = document.createElement('span');
  meta.className = 'muted small';
  meta.textContent = `${user.role} · ${user.user_id}${user.on_sensor ? '' : ' · roster only'}`;
  identity.append(name, meta);
  const del = document.createElement('button');
  del.className = 'btn small ghost';
  del.textContent = 'Delete';
  del.disabled = busy;
  del.addEventListener('click', async () => {
    if (!window.confirm(`Delete ${user.name} (${user.role}) from the F455?`)) return;
    setBusy(el, true);
    setStatus(el, `Deleting ${user.name}…`, '#d29922');
    try {
      const response = await request(`/v1/face/users/${encodeURIComponent(user.user_id)}`, { method: 'DELETE' });
      showResult(el, { ...(await response.json()), message: `Deleted ${user.name}` });
      await refreshUsers(el);
    } catch (e) { setStatus(el, e.message, '#f85149'); }
    finally { setBusy(el, false); }
  });
  row.append(identity, del);
  return row;
}

async function refreshUsers(el) {
  const data = await getJSON('/v1/face/users');
  const users = el.querySelector('#face-users');
  users.replaceChildren();
  if (!data.users.length) {
    const empty = document.createElement('div');
    empty.className = 'muted';
    empty.textContent = 'No enrolled users.';
    users.append(empty);
  } else {
    for (const user of data.users) users.append(userRow(el, user));
  }
  el.querySelector('#face-delete-all').disabled = busy || !data.users.length;
}

async function runEnroll(el, name, role, confirm = false) {
  return postJSON('/v1/face/enroll', { name, role, ...(confirm ? { confirm: true } : {}) });
}

export function init(el) {
  el.innerHTML = `
    <h1>Face ID</h1>
    <p class="lede">Enrollment, encrypted templates and anti-spoofing run on the Intel RealSense ID F455. Images and faceprints stay on the device.</p>
    <div class="face-layout">
      <div class="card face-preview-card">
        <div class="card-head"><h3>Live F455 face tracking</h3><span class="row"><span class="tag" id="face-preview-state">Connecting…</span><button class="btn small" id="face-preview-toggle">Stop</button></span></div>
        <div style="position:relative">
          <canvas id="face-preview" style="width:100%;display:block;object-fit:contain;background:#0b0d10;border-radius:12px"></canvas>
          <div id="face-preview-ph" class="placeholder" style="border-radius:12px" hidden>Stopped — press Start</div>
        </div>
        <p class="muted small">The white oval follows the Hailo SCRFD face box. It turns green after authentication; matching and anti-spoofing always run inside the F455.</p>
      </div>
      <div class="face-controls">
        <div class="two">
          <div class="card">
            <div class="card-head"><h3>Authenticate</h3><span class="tag green">On-device</span></div>
            <div id="face-mode" class="muted">Device mode appears after authentication.</div>
            <p class="muted">Look at the F455, then start authentication.</p>
            <button class="btn" id="face-auth">Authenticate</button>
            <div id="face-auth-status" style="margin-top:12px;font-weight:700">Ready.</div>
            <details style="margin-top:10px"><summary class="muted">Diagnostics</summary><pre id="face-result" class="log"></pre></details>
          </div>
          <div class="card">
            <div class="card-head"><h3>Enroll a person</h3><span class="tag">Driver or passenger</span></div>
            <form id="face-enroll" class="face-enroll-grid">
              <input id="face-name" placeholder="Full name" maxlength="24" required>
              <select id="face-role" aria-label="Role"><option value="driver">Driver</option><option value="passenger">Passenger</option></select>
              <button class="btn" id="face-enroll-submit" type="submit">Enroll</button>
            </form>
            <p class="muted small">A new driver replaces the current driver only after confirmation. Duplicate faces and duplicate name/role pairs are rejected.</p>
          </div>
        </div>
        <div class="card">
          <div class="card-head"><h3>Enrolled users</h3><span class="row"><button class="btn small ghost" id="face-refresh">Refresh</button><button class="btn small ghost" id="face-delete-all">Delete all</button></span></div>
          <div id="face-users" class="face-users"><span class="muted">Loading…</span></div>
        </div>
      </div>
    </div>`;

  root = el;
  el.querySelector('#face-preview-toggle').addEventListener('click', () => { if (ws) stopPreview(el); else startPreview(el); });
  el.querySelector('#face-refresh').addEventListener('click', () => { refreshUsers(el).catch(e => setStatus(el, e.message, '#f85149')); });
  el.querySelector('#face-auth').addEventListener('click', async () => {
    if (busy) return;
    setBusy(el, true);
    setStatus(el, 'Authenticating… keep looking at the camera.', '#d29922');
    try { showResult(el, await postJSON('/v1/face/authenticate', {})); }
    catch (e) { setStatus(el, e.message, '#f85149'); }
    finally { setBusy(el, false); }
  });
  el.querySelector('#face-enroll').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (busy) return;
    const name = el.querySelector('#face-name').value.trim();
    const role = el.querySelector('#face-role').value;
    setBusy(el, true);
    setStatus(el, `Enrolling ${name}… follow the camera hints.`, '#d29922');
    try {
      let data;
      try { data = await runEnroll(el, name, role); }
      catch (e) {
        if (e.data?.error?.code !== 'driver_exists') throw e;
        const existing = e.data.existing?.name || 'the current driver';
        if (!window.confirm(`Replace ${existing} with ${name} as the driver? This deletes the old driver template.`)) {
          setStatus(el, 'Driver replacement cancelled.');
          return;
        }
        setStatus(el, `Replacing ${existing} with ${name}…`, '#d29922');
        data = await runEnroll(el, name, role, true);
      }
      showResult(el, data);
      if (data.enrolled) el.querySelector('#face-name').value = '';
      await refreshUsers(el);
    } catch (e) { setStatus(el, e.message, '#f85149'); }
    finally { setBusy(el, false); }
  });
  el.querySelector('#face-delete-all').addEventListener('click', async () => {
    if (busy || !window.confirm('Delete every enrolled face template and roster entry? This cannot be undone.')) return;
    setBusy(el, true);
    setStatus(el, 'Deleting all enrolled users…', '#d29922');
    try {
      const response = await request('/v1/face/users', { method: 'DELETE' });
      showResult(el, { ...(await response.json()), message: 'Deleted all enrolled users' });
      await refreshUsers(el);
    } catch (e) { setStatus(el, e.message, '#f85149'); }
    finally { setBusy(el, false); }
  });
  refreshUsers(el).catch(e => setStatus(el, e.message, '#f85149'));
  startPreview(el);
}

export function show() {
  if (!root) return;
  if (!ws) startPreview(root);
  refreshUsers(root).catch(e => setStatus(root, e.message, '#f85149'));
}

export function hide() {
  if (root) stopPreview(root);
}
