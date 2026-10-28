// HTTP / WS / SSE helpers for the AI Box API. Key comes from localStorage.aiboxKey.
export const apiBase = location.origin;
export const bus = new EventTarget();
// auth.open is true while the key prompt is showing; pollers and retries wait on it.
export const auth = { open: false };
export async function whileAuthOpen() { while (auth.open) await new Promise((r) => setTimeout(r, 500)); }
function needAuth() {
  if (auth.open) return;
  auth.open = true;
  bus.dispatchEvent(new CustomEvent('auth-required'));
}

export function getKey() {
  try { return localStorage.getItem('aiboxKey') || ''; } catch { return ''; }
}
export function setKey(k) {
  try { localStorage.setItem('aiboxKey', k); } catch { /* storage blocked */ }
}

function headers(extra = {}) {
  const k = getKey();
  return k ? { Authorization: `Bearer ${k}`, ...extra } : { ...extra };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// fetch with 401 -> key prompt event and 503 + Retry-After -> swap banner + retry
export async function request(path, opts = {}, retries = 6) {
  for (let attempt = 0; ; attempt++) {
    let res;
    try {
      res = await fetch(apiBase + path, { ...opts, headers: headers(opts.headers) });
    } catch (e) {
      throw new Error(`Network error: ${e.message}`);
    }
    if (res.status === 401) {
      needAuth();
      throw new Error('401: API key required');
    }
    if (res.status === 503 && attempt < retries) {
      const wait = Math.max(1, Number(res.headers.get('Retry-After')) || 3);
      bus.dispatchEvent(new CustomEvent('swap', { detail: { retry_after: wait } }));
      await sleep(wait * 1000);
      continue;
    }
    if (!res.ok) {
      let msg = `${res.status}`;
      let data = null;
      try {
        data = await res.json();
        msg = data.error?.message || data.detail || JSON.stringify(data);
      } catch { /* not JSON */ }
      const error = new Error(msg);
      error.status = res.status;
      error.data = data;
      throw error;
    }
    return res;
  }
}

export async function getJSON(path) {
  return (await request(path)).json();
}

export async function postJSON(path, body) {
  return (await request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })).json();
}

export async function postForm(path, form) {
  return (await request(path, { method: 'POST', body: form })).json();
}

export function wsURL(path) {
  const u = new URL(path, location.href);
  u.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const k = getKey();
  if (k) u.searchParams.set('key', k);
  return u.toString();
}

export function openWS(path) {
  const ws = new WebSocket(wsURL(path));
  ws.binaryType = 'arraybuffer';
  return ws;
}

// Streaming reader for text/event-stream. Reconnects unless `opts.once`.
// Returns {close()}. Uses fetch so the Bearer header works; POST bodies supported.
export function sse(path, onRow, opts = {}) {
  const ctl = new AbortController();
  let closed = false;
  (async () => {
    while (!closed) {
      await whileAuthOpen();
      try {
        const res = await fetch(apiBase + path, {
          method: opts.body ? 'POST' : 'GET',
          headers: headers(opts.body ? { 'Content-Type': 'application/json' } : {}),
          body: opts.body ? JSON.stringify(opts.body) : undefined,
          signal: ctl.signal,
        });
        if (res.status === 401) needAuth();
        if (res.status === 503) {
          bus.dispatchEvent(new CustomEvent('swap', {
            detail: { retry_after: Number(res.headers.get('Retry-After')) || 3 } }));
        }
        if (!res.ok) throw new Error(`${res.status}`);
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.search(/\r?\n\r?\n/)) >= 0) {
            const block = buf.slice(0, i);
            buf = buf.slice(i).replace(/^\r?\n\r?\n/, '');
            const data = block.split(/\r?\n/).filter((l) => l.startsWith('data:'))
              .map((l) => l.slice(5).replace(/^ /, '')).join('\n');
            if (!data) continue;
            if (data === '[DONE]') { onRow(null, true); continue; }
            try { onRow(JSON.parse(data), false); } catch { /* ignore bad row */ }
          }
        }
      } catch (e) {
        if (closed) return;
        if (opts.once) { opts.onError?.(e); return; }
      }
      if (closed || opts.once) return;
      await sleep(2000);
    }
  })();
  return { close() { closed = true; ctl.abort(); } };
}

// Parse an SSE response body, calling onRow(obj) per data row ([DONE] -> onRow(null, true)).
export async function readSSE(res, onRow) {
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.search(/\r?\n\r?\n/)) >= 0) {
      const block = buf.slice(0, i);
      buf = buf.slice(i).replace(/^\r?\n\r?\n/, '');
      const data = block.split(/\r?\n/).filter((l) => l.startsWith('data:'))
        .map((l) => l.slice(5).replace(/^ /, '')).join('\n');
      if (!data) continue;
      if (data === '[DONE]') { onRow(null, true); continue; }
      try { onRow(JSON.parse(data), false); } catch { /* ignore bad row */ }
    }
  }
}
