import { bus, getKey, setKey, auth } from './api.js';
import { startFeeds, state, $ } from './state.js';

const PAGES = {
  overview: { mod: './overview.js', eyebrow: 'EDGE AI / INFERENCE SERVER' },
  voice: { mod: './voice.js', eyebrow: 'APPLICATION 01 / VOICE INTELLIGENCE' },
  vision: { mod: './vision.js', eyebrow: 'APPLICATION 02 / VISUAL INTELLIGENCE' },
  chat: { mod: './chat.js', eyebrow: 'APPLICATION 03 / LANGUAGE MODELS' },
  face: { mod: './face.js', eyebrow: 'APPLICATION 04 / REALSENSE ID' },
  device: { mod: './device.js', eyebrow: 'SYSTEM / DEVICE STATUS' },
  inspector: { mod: './inspector.js', eyebrow: 'SYSTEM / REQUEST INSPECTOR' },
};
const loaded = {};
let current = null;

async function show(name) {
  if (!PAGES[name]) name = 'overview';
  if (current && loaded[current]?.hide) loaded[current].hide();
  current = name;
  for (const p of Object.keys(PAGES)) $(`#page-${p}`).hidden = p !== name;
  for (const a of document.querySelectorAll('#nav a')) a.classList.toggle('active', a.dataset.page === name);
  $('#eyebrow').textContent = PAGES[name].eyebrow;
  if (!loaded[name]) {
    try {
      loaded[name] = await import(PAGES[name].mod);
      loaded[name].init?.($(`#page-${name}`));
    } catch (e) {
      loaded[name] = {};
      $(`#page-${name}`).innerHTML = '';
      const d = document.createElement('div');
      d.className = 'banner err';
      d.textContent = `This page failed to load: ${e.message}`;
      $(`#page-${name}`).append(d);
    }
  }
  loaded[name].show?.();
}

function route() { show((location.hash || '#overview').slice(1)); }

// online pill + sidebar footer
bus.addEventListener('online', (e) => {
  const on = e.detail;
  $('#pill').className = `pill ${on ? 'on' : 'off'}`;
  $('#pill-text').textContent = on ? 'Server Online' : 'Server Offline';
  $('#foot-dot').className = `dot ${on ? 'on' : ''}`;
  $('#foot-text').textContent = on ? 'Connected' : 'Unreachable';
});

// swap banner: driven by system stream and by 503 retries
let swapTimer = null;
function swap(on) { $('#swap-banner').hidden = !on; }
bus.addEventListener('genai', (e) => {
  if (e.detail.state === 'swapping') swap(true);
  else if (!swapTimer) swap(false);
});
bus.addEventListener('swap', (e) => {
  swap(true);
  clearTimeout(swapTimer);
  swapTimer = setTimeout(() => { swapTimer = null; if (state.snap?.npu?.genai_state !== 'swapping') swap(false); },
    (e.detail.retry_after || 3) * 1000 + 1500);
});

// API key dialog (no browser prompt())
const modal = $('#key-modal');
function askKey() {
  auth.open = true;
  if (!modal.hidden) return;
  $('#key-input').value = getKey();
  modal.hidden = false;
  $('#key-input').focus();
}
bus.addEventListener('auth-required', askKey);
$('#key-btn').addEventListener('click', askKey);
$('#key-cancel').addEventListener('click', () => { modal.hidden = true; auth.open = false; });
$('#key-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  setKey($('#key-input').value.trim());
  auth.open = false;
  location.reload();
});

// mic / webcam need a secure context
if (!window.isSecureContext) {
  const b = $('#https-banner');
  const httpsUrl = `https://${location.hostname}:8443/`;
  b.hidden = false;
  b.textContent = `Microphone and camera need HTTPS. Open ${httpsUrl} (accept the self-signed certificate once). Chat, vision from the box cameras and uploads work here.`;
}

window.addEventListener('hashchange', route);
startFeeds();
route();
