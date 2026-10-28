import { openWS, getJSON } from './api.js';
import { h, $, num, modelsList } from './state.js';
import { NovaAvatar, ST } from './avatar.js';

// ---- VAD / protocol constants (addendum §2) ----
const FRAME_MS = 20;                 // worklet emits 320-sample frames at 16 kHz
const START_FRAMES = 150 / FRAME_MS; // speech must hold 150 ms above the threshold
const END_FRAMES = 700 / FRAME_MS;   // utterance ends after 700 ms below it
const PREROLL_FRAMES = (300 + 150) / FRAME_MS; // 300 ms of pre-roll plus the detection window
const CALIB_FRAMES = 1000 / FRAME_MS;
const MIN_THRESHOLD = 0.012, MAX_THRESHOLD = 0.08;
const MAX_UTTER_FRAMES = 30000 / FRAME_MS;
const GATE_TAIL_MS = 400;
const DEFAULT_MODEL = 'qwen3-vl';

// AudioWorklet: windowed-sinc low-pass (7.2 kHz) then decimate to 16 kHz, emitting 20 ms PCM16 frames.
const WORKLET = `
class Down extends AudioWorkletProcessor {
  constructor() {
    super();
    this.r = Math.max(1, sampleRate / 16000);
    const fc = Math.min(7200 / sampleRate, 0.5);
    this.half = this.r > 1 ? Math.ceil(this.r * 6) : 0;
    const n = 2 * this.half + 1;
    this.taps = new Float32Array(n);
    let sum = 0;
    for (let i = 0; i < n; i++) {
      const x = i - this.half;
      const sinc = x === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * x) / (Math.PI * x);
      const win = n > 1 ? 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1)) : 1;
      this.taps[i] = sinc * win; sum += this.taps[i];
    }
    for (let i = 0; i < n; i++) this.taps[i] /= sum;
    this.hist = new Float32Array(n); this.pos = 0; this.acc = 0;
    this.frame = new Int16Array(320); this.fill = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    const n = this.taps.length;
    for (let i = 0; i < ch.length; i++) {
      this.hist[this.pos] = ch[i];
      this.pos = (this.pos + 1) % n;
      this.acc += 1;
      if (this.acc >= this.r) {
        this.acc -= this.r;
        let y = 0;
        for (let k = 0; k < n; k++) y += this.taps[k] * this.hist[(this.pos - 1 - k + n * 2) % n];
        this.frame[this.fill++] = Math.max(-1, Math.min(1, y)) * 32767;
        if (this.fill === 320) {
          const out = this.frame.slice();
          this.port.postMessage(out.buffer, [out.buffer]);
          this.fill = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('down16', Down);`;

const V = {
  phase: 'idle',           // idle | listening | thinking | speaking
  muted: false, ptt: false,
  ws: null, wsWanted: false, reconnect: 0,
  ctx: null, stream: null, node: null, src: null, analyser: null, attached: false,
  micOn: false, starting: false,
  model: DEFAULT_MODEL,
  calib: [], thr: 0.02, calibrated: false,
  speech: false, above: 0, below: 0, utterFrames: 0, preroll: [],
  gateUntil: 0, level: 0,
  ignore: false, cancelTimer: 0, doneTimer: 0,
  wav: [], url: null, replyEl: null, gotAudio: false,
};
let el = {}, avatar = null, meterTimer = 0, initialized = false;

// ---- UI helpers ----
const LABELS = { idle: 'Ready — just speak', listening: 'Listening…', thinking: 'Thinking…', speaking: 'Speaking…' };

function showError(msg) { el.err.hidden = !msg; el.err.textContent = msg || ''; }

function render() {
  let label;
  if (!V.micOn && !V.starting) label = el.mic_blocked ? 'Microphone unavailable' : 'Mic off';
  else if (V.starting) label = 'Starting mic…';
  else if (V.muted && (V.phase === 'idle' || V.phase === 'listening')) label = 'Muted';
  else if (V.phase === 'idle' && !V.calibrated) label = 'Calibrating room noise…';
  else label = LABELS[V.phase];
  el.state.textContent = label;
  el.state.dataset.phase = V.muted && V.phase === 'idle' ? 'muted' : V.phase;
  const busy = V.phase === 'thinking' || V.phase === 'speaking';
  const av = V.muted && !busy ? ST.MUTED : { idle: ST.IDLE, listening: ST.LISTENING, thinking: ST.THINKING, speaking: ST.SPEAKING }[V.phase];
  avatar?.setState(av);
  el.mute.textContent = V.muted ? 'Unmute' : 'Mute';
  el.mute.setAttribute('aria-pressed', String(V.muted));
  el.mute.classList.toggle('active', V.muted);
  el.stop.disabled = V.phase === 'idle' && !V.speech;
  el.ptt.disabled = busy;
  el.ptt.classList.toggle('rec', V.ptt);
  el.stage.dataset.phase = av;
}

function setPhase(p) { V.phase = p; render(); }

function bubble(kind, text) {
  const b = h('div', { class: `vbubble ${kind}` }, text);
  el.convo.append(b);
  while (el.convo.children.length > 24) el.convo.firstChild.remove();
  el.convo.scrollTop = el.convo.scrollHeight;
  return b;
}

// ---- WebSocket ----
function connect() {
  if (V.ws && V.ws.readyState <= 1) return;
  const w = openWS('/v1/voice/stream');
  V.ws = w;
  w.onopen = () => { V.reconnect = 0; if (el.err.dataset.kind === 'net') showError(''); };
  w.onclose = () => {
    if (V.ws === w) V.ws = null;
    if (V.wsWanted) setTimeout(connect, Math.min(4000, 500 * 2 ** V.reconnect++));
  };
  w.onerror = () => { el.err.dataset.kind = 'net'; showError('Voice stream unreachable — retrying…'); };
  w.onmessage = (ev) => { if (V.ws === w) onMessage(ev); };
}

function send(obj) { if (V.ws?.readyState === 1) { V.ws.send(JSON.stringify(obj)); return true; } return false; }

function onMessage(ev) {
  if (typeof ev.data !== 'string') {
    if (V.ignore) return;
    V.wav.push(ev.data); maybePlay();
    return;
  }
  let m; try { m = JSON.parse(ev.data); } catch { return; }
  if (m.type === 'cancelled') { endIgnore(); setPhase('idle'); return; }
  if (m.type === 'error') {
    const msg = m.error?.message || m.error || m.message || 'voice error';
    const code = m.error?.code;
    if (V.ignore) {                 // the server does not understand cancel: drop the socket to end the turn
      endIgnore(); try { V.ws?.close(); } catch { /* closed */ }
      return;
    }
    if (code !== 'empty_audio') { el.err.dataset.kind = 'voice'; showError(msg); }
    clearTimeout(V.doneTimer); setPhase('idle');
    return;
  }
  if (V.ignore) return;
  if (m.type === 'transcript') {
    const t = (m.text || '').trim();
    bubble('user', t || '(nothing heard)');
    V.replyEl = t ? bubble('nova', '') : null;
    V.wav.length = 0; V.gotAudio = false;
    if (!t) setPhase('idle');
  } else if (m.type === 'token') {
    if (!V.replyEl) V.replyEl = bubble('nova', '');
    V.replyEl.textContent += m.text;
    el.convo.scrollTop = el.convo.scrollHeight;
  } else if (m.type === 'done') {
    if (V.replyEl && !V.replyEl.textContent && m.text) V.replyEl.textContent = m.text;
    const t = m.timings || {};
    el.timings.replaceChildren(...[['ASR', t.asr_ms], ['LLM TTFT', t.llm_ttft_ms], ['LLM', t.llm_ms], ['TTS', t.tts_ms]]
      .map(([k, v]) => h('span', { class: 'tag blue' }, `${k} ${num(v, 0)} ms`)));
    // no reply text means no audio will follow; otherwise wait a while for the WAV
    clearTimeout(V.doneTimer);
    V.doneTimer = setTimeout(() => { if (V.phase === 'thinking' && !V.gotAudio) setPhase('idle'); }, m.text ? 8000 : 0);
  }
}

function endIgnore() { V.ignore = false; clearTimeout(V.cancelTimer); }

// ---- playback ----
function wavTotal(first) {
  if (!first || first.byteLength < 8) return null;
  return new DataView(first).getUint32(4, true) + 8;   // RIFF size field + 8
}
function maybePlay() {
  const total = wavTotal(V.wav[0]);
  const have = V.wav.reduce((n, p) => n + p.byteLength, 0);
  if (total == null || have < total) return;
  const url = URL.createObjectURL(new Blob(V.wav, { type: 'audio/wav' }));
  V.wav = []; V.gotAudio = true;
  if (V.url) URL.revokeObjectURL(V.url);
  V.url = url;
  const a = el.audio;
  if (V.ctx && V.ctx.state === 'running' && !V.attached) {
    try {
      const s = V.ctx.createMediaElementSource(a);
      V.analyser = V.ctx.createAnalyser(); V.analyser.fftSize = 1024;
      s.connect(V.analyser); V.analyser.connect(V.ctx.destination);
      V.attached = true;
    } catch { /* already attached */ }
  }
  a.onended = () => { V.gateUntil = performance.now() + GATE_TAIL_MS; resetVad(); setPhase('idle'); avatar?.setLevel(true, 0); };
  a.src = url;
  setPhase('speaking');
  a.play().catch(() => { setPhase('idle'); });
}

function stopPlayback() {
  const a = el.audio;
  a.onended = null;
  try { a.pause(); a.removeAttribute('src'); a.load(); } catch { /* ignore */ }
  V.wav = [];
  avatar?.setLevel(true, 0);
}

// ---- capture + VAD ----
function rmsOf(pcm) {
  let s = 0;
  for (let i = 0; i < pcm.length; i++) { const v = pcm[i] / 32768; s += v * v; }
  return Math.sqrt(s / pcm.length);
}

function resetVad() { V.speech = false; V.above = 0; V.below = 0; V.utterFrames = 0; V.preroll = []; }

function beginUtterance() {
  V.speech = true; V.below = 0; V.utterFrames = 0;
  send({ type: 'start', model: V.model });
  for (const f of V.preroll) V.ws?.readyState === 1 && V.ws.send(f);
  V.utterFrames = V.preroll.length;
  V.preroll = [];
  setPhase('listening');
}

function endUtterance() {
  V.speech = false; V.preroll = []; V.above = 0; V.below = 0;
  clearTimeout(V.doneTimer);
  if (send({ type: 'stop' })) setPhase('thinking'); else setPhase('idle');
}

function onFrame(buf) {
  const pcm = new Int16Array(buf), rms = rmsOf(pcm), now = performance.now();
  V.level = rms;
  if (V.phase === 'thinking' || V.phase === 'speaking') return;
  if (V.ptt) {
    if (V.ws?.readyState === 1) { V.ws.send(buf); V.utterFrames++; }
    return;
  }
  if (V.muted) { V.preroll = []; V.above = 0; return; }
  if (now < V.gateUntil) return;
  if (!V.calibrated) {
    V.calib.push(rms);
    if (V.calib.length >= CALIB_FRAMES) {
      const sorted = [...V.calib].sort((a, b) => a - b), med = sorted[sorted.length >> 1];
      V.thr = Math.min(MAX_THRESHOLD, Math.max(MIN_THRESHOLD, med * 3));
      V.calibrated = true; render();
    }
    return;
  }
  if (!V.speech) {
    V.preroll.push(buf);
    if (V.preroll.length > PREROLL_FRAMES) V.preroll.shift();
    V.above = rms > V.thr ? V.above + 1 : 0;
    if (V.above >= START_FRAMES && V.ws?.readyState === 1) beginUtterance();
    return;
  }
  if (V.ws?.readyState === 1) V.ws.send(buf);
  V.utterFrames++;
  V.below = rms < V.thr ? V.below + 1 : 0;
  if (V.below >= END_FRAMES || V.utterFrames >= MAX_UTTER_FRAMES) endUtterance();
}

async function startMic() {
  if (V.micOn || V.starting) return;
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    el.mic_blocked = true;
    el.https.hidden = false;
    el.https.textContent = `The microphone needs HTTPS. Open https://${location.hostname}:8443/ and accept the self-signed certificate once. You can still watch the avatar; chat and vision work over HTTP.`;
    render();
    return;
  }
  V.starting = true; render();
  try {
    V.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: false } });
    V.ctx = new AudioContext();
    await V.ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: 'text/javascript' })));
    V.src = V.ctx.createMediaStreamSource(V.stream);
    V.node = new AudioWorkletNode(V.ctx, 'down16');
    V.node.port.onmessage = (e) => onFrame(e.data);
    V.src.connect(V.node);
    V.stream.getAudioTracks().forEach((t) => { t.enabled = !V.muted; });
    if (V.ctx.state !== 'running') {
      const resume = () => { V.ctx?.resume(); document.removeEventListener('pointerdown', resume); document.removeEventListener('keydown', resume); };
      document.addEventListener('pointerdown', resume); document.addEventListener('keydown', resume);
      V.ctx.resume().catch(() => {});
    }
    V.micOn = true; el.mic_blocked = false; el.https.hidden = true;
    V.calib = []; V.calibrated = false; resetVad();
  } catch (e) {
    releaseMic();
    el.mic_blocked = true;
    el.err.dataset.kind = 'mic'; showError(`Cannot open the microphone: ${e.message}`);
  }
  V.starting = false; render();
}

function releaseMic() {
  try { V.node?.disconnect(); V.src?.disconnect(); } catch { /* ignore */ }
  V.node = null; V.src = null;
  V.stream?.getTracks().forEach((t) => t.stop()); V.stream = null;
  V.ctx?.close().catch(() => {}); V.ctx = null; V.analyser = null; V.attached = false;
  V.micOn = false;
}

// ---- controls ----
function toggleMute() {
  V.muted = !V.muted;
  V.stream?.getAudioTracks().forEach((t) => { t.enabled = !V.muted; });
  if (V.muted && V.speech) { send({ type: 'cancel' }); markCancel(); resetVad(); setPhase('idle'); }
  V.preroll = []; V.above = 0;
  render();
}

function markCancel() {
  V.ignore = true;
  clearTimeout(V.cancelTimer);
  // a server without cancel never replies: end the turn by dropping the socket instead
  const w = V.ws;
  V.cancelTimer = setTimeout(() => { if (V.ignore) { V.ignore = false; if (V.ws === w) try { w?.close(); } catch { /* closed */ } } }, 1500);
}

function stopTurn() {
  stopPlayback();
  clearTimeout(V.doneTimer);
  if (V.replyEl) { V.replyEl.classList.add('stopped'); V.replyEl = null; }
  if (V.ws?.readyState === 1) { send({ type: 'cancel' }); markCancel(); }
  V.ptt = false;
  resetVad();
  V.gateUntil = performance.now() + GATE_TAIL_MS;
  setPhase('idle');
}

function pttDown() {
  if (V.phase === 'thinking' || V.phase === 'speaking' || V.ptt) return;
  if (!V.micOn) { startMic(); return; }
  if (V.ws?.readyState !== 1) return;
  V.ptt = true; V.speech = true;
  V.stream?.getAudioTracks().forEach((t) => { t.enabled = true; });
  send({ type: 'start', model: V.model });
  for (const f of V.preroll) V.ws.send(f);
  V.preroll = []; V.utterFrames = 0;
  setPhase('listening');
}
function pttUp() {
  if (!V.ptt) return;
  V.ptt = false;
  V.stream?.getAudioTracks().forEach((t) => { t.enabled = !V.muted; });
  endUtterance();
}

// ---- meters ----
function tickLevels() {
  if (V.phase === 'speaking' && V.analyser) {
    const buf = new Float32Array(V.analyser.fftSize);
    V.analyser.getFloatTimeDomainData(buf);
    let s = 0; for (const v of buf) s += v * v;
    avatar.setLevel(true, Math.min(1, Math.sqrt(s / buf.length) * 5));
  }
  const mic = (V.phase === 'idle' || V.phase === 'listening') && !V.muted ? V.level : 0;
  avatar.setLevel(false, mic > 0.004 ? Math.min(1, mic * 7) : 0);
  el.meter.style.width = `${Math.min(100, mic * 700)}%`;
  el.meter.parentElement.classList.toggle('over', V.calibrated && mic > V.thr);
}

async function loadModels() {
  let ids = [];
  try { ids = modelsList(await getJSON('/v1/models')).filter((m) => ['chat', 'vlm'].includes(m.modality || 'chat')).map((m) => m.id); } catch { /* offline */ }
  if (!ids.length) ids = [DEFAULT_MODEL, 'qwen2.5:1.5b', 'qwen3:1.7b', 'llama3.2:1b', 'qwen2:1.5b'];
  else if (!ids.includes(DEFAULT_MODEL)) ids.unshift(DEFAULT_MODEL);
  el.model.replaceChildren(...ids.map((m) => h('option', { value: m }, m)));
  V.model = ids.includes(DEFAULT_MODEL) ? DEFAULT_MODEL : ids[0];
  el.model.value = V.model;
}

export function init(root) {
  if (initialized) return;
  initialized = true;
  root.append(
    h('div', { class: 'voice-head' },
      h('div', {}, h('h1', {}, 'Voice AI'),
        h('p', { class: 'lede' }, 'Just speak. Whisper runs on the NPU, a local LLM replies and Piper speaks it. Stop ends the reply instantly.')),
      h('label', { class: 'muted small voice-model' }, 'Reply model ', h('select', { id: 'vo-model' }, h('option', {}, DEFAULT_MODEL)))),
    h('div', { class: 'banner err', id: 'v-err', hidden: true }),
    h('div', { class: 'banner warn', id: 'v-https', hidden: true }),
    h('div', { class: 'voice-stage', id: 'vo-stage' },
      h('canvas', { id: 'vo-avatar', class: 'nova-avatar', role: 'img', 'aria-label': 'Nova avatar' }),
      h('div', { class: 'vo-state', id: 'vo-state', 'aria-live': 'polite' }, 'Mic off'),
      h('div', { class: 'vo-meter', title: 'Microphone level' }, h('i', { id: 'vo-meter' })),
      h('div', { class: 'vo-controls' },
        h('button', { class: 'btn vo-btn', id: 'vo-mute', 'aria-pressed': 'false' }, 'Mute'),
        h('button', { class: 'btn vo-btn danger', id: 'vo-stop', disabled: true, title: 'Esc' }, 'Stop'),
        h('button', { class: 'btn vo-btn', id: 'vo-ptt', title: 'Hold (or hold Space) in a noisy room' }, 'Hold to talk'))),
    h('div', { class: 'vo-convo', id: 'vo-convo', 'aria-live': 'polite' }),
    h('div', { class: 'timings', id: 'vo-timings' }),
    h('audio', { id: 'vo-audio', hidden: true }));
  el = { err: $('#v-err'), https: $('#v-https'), stage: $('#vo-stage'), state: $('#vo-state'), meter: $('#vo-meter'),
    mute: $('#vo-mute'), stop: $('#vo-stop'), ptt: $('#vo-ptt'), convo: $('#vo-convo'), timings: $('#vo-timings'),
    audio: $('#vo-audio'), model: $('#vo-model') };
  avatar = new NovaAvatar($('#vo-avatar'));
  avatar.start();
  loadModels();
  el.model.addEventListener('change', (e) => { V.model = e.target.value; });
  el.mute.addEventListener('click', toggleMute);
  el.stop.addEventListener('click', stopTurn);
  const p = el.ptt;
  p.addEventListener('pointerdown', (e) => { e.preventDefault(); p.setPointerCapture?.(e.pointerId); pttDown(); });
  p.addEventListener('pointerup', pttUp);
  p.addEventListener('pointercancel', pttUp);
  p.addEventListener('contextmenu', (e) => e.preventDefault());
  const active = () => !$('#page-voice').hidden;
  document.addEventListener('keydown', (e) => {
    if (!active()) return;
    if (e.code === 'Escape') stopTurn();
    else if (e.target === document.body && e.code === 'Space' && !e.repeat) { e.preventDefault(); pttDown(); }
    else if (e.target === document.body && e.code === 'KeyM' && !e.repeat) toggleMute();
  });
  document.addEventListener('keyup', (e) => { if (e.code === 'Space' && active()) pttUp(); });
  meterTimer = setInterval(() => { if (active()) tickLevels(); }, 40);
  render();
}

export function show() {
  if (!initialized) return;
  V.wsWanted = true; connect();
  startMic();
  avatar.start();
}

export function hide() {
  if (!initialized) return;
  stopTurn();
  endIgnore();
  V.wsWanted = false;
  try { V.ws?.close(); } catch { /* closed */ }
  releaseMic();
  V.calibrated = false; V.calib = [];
  avatar.stop();
  render();
}

void meterTimer;
