// NovaBot face for the browser: a port of cb_lvgl/src/nova/avatar_model.c (pose model) and avatar.c (pebble head,
// two eyes). Sizes are fractions of the head radius R = 0.27 * box; hue is degrees, sat percent.
const TAU_MS = 140, BLINK_MS = 160, EYE_SCALE = 1.4, GAP = 0.27;
const clamp = (v, lo, hi) => (v < lo ? lo : v > hi ? hi : v);

export const ST = { IDLE: 'idle', LISTENING: 'listening', THINKING: 'thinking', SPEAKING: 'speaking', MUTED: 'muted' };

const REST = { eyeW: 0.15, eyeH: 0.32, lidT: 0.08, lidB: 0, happy: 0, lookX: 0, lookY: 0, tilt: 0, asym: 0, lean: 0,
  hue: 196, sat: 40, spark: 0, bob: 1 };

export function stateBase(s) {
  const p = { ...REST };
  if (s === ST.LISTENING) Object.assign(p, { eyeW: 0.165, eyeH: 0.32, hue: 202, sat: 55, lean: -0.05, lidT: 0.08, lookX: -0.03 });
  else if (s === ST.THINKING) Object.assign(p, { eyeH: 0.24, lidT: 0, lookY: -0.09, lidB: 0.14, hue: 258, sat: 34, bob: 0.5 });
  else if (s === ST.SPEAKING) Object.assign(p, { eyeH: 0.30, happy: 0.15, lidT: 0.10, hue: 164, sat: 40 });
  else if (s === ST.MUTED) Object.assign(p, { eyeW: 0.18, eyeH: 0.045, lidT: 0, hue: 214, sat: 5, bob: 0.3 });
  return p;
}

// Small mood offsets (the C personality() table is still a TODO there): additive happy/look/lid, replaced size.
const MOODS = {
  calm: {}, happy: { happy: 0.5 }, curious: { lookY: -0.04, eyeH: 0.34 }, concerned: { lidT: 0.12, lookY: 0.03 },
  surprised: { eyeH: 0.38, lidT: 0 }, sleepy: { lidT: 0.35 },
};
const REPLACED = ['eyeW', 'eyeH', 'hue', 'sat', 'bob'];

export function targetPose(state, mood = 'calm') {
  const b = stateBase(state), d = MOODS[mood] || {}, t = { ...b };
  const muted = state === ST.MUTED;
  t.happy = clamp(b.happy + (d.happy || 0), 0, 1);
  t.lidT = clamp(b.lidT + (d.lidT || 0), 0, 1);
  t.lidB = clamp(b.lidB + (d.lidB || 0), 0, 1);
  t.lookX = b.lookX + (d.lookX || 0);
  t.lookY = b.lookY + (d.lookY || 0);
  if (!muted) { for (const k of REPLACED) if (d[k] != null) t[k] = d[k]; }
  else { t.happy = 0; t.lidT = 0; }
  return t;
}

const envelope = (t, seed) => {
  const s = Math.sin(t * 9.3 + seed) * Math.sin(t * 3.1 + seed * 2) + 0.35 * Math.sin(t * 17.7 + seed);
  return clamp(0.25 + 0.75 * Math.abs(s), 0, 1);
};
const hueDiff = (a, b) => ((b - a + 540) % 360) - 180;

export function eyeMorph(eyeH, happy) {
  const pillOpa = happy < 0.45 ? 1 : 0;
  return { pillH: eyeH * (1 - 0.9 * clamp((happy - 0.2) / 0.25, 0, 1)), pillOpa, archOpa: 1 - pillOpa,
    archRise: clamp((happy - 0.45) / 0.2, 0, 1) };
}

export class NovaAvatar {
  constructor(canvas) {
    this.cv = canvas;
    this.state = ST.IDLE; this.mood = 'calm';
    this.pose = targetPose(this.state, this.mood);
    this.scan = 0; this.driver = 0; this.nova = 0; this.last = 0;
    this.nextBlink = 3000 + Math.random() * 3000; this.blinking = false;
    this.reduce = !!matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    this.raf = 0; this.t0 = performance.now();
    this.frameCount = 0;
  }
  setState(s) { this.state = s; if (s === ST.MUTED) this.blinking = false; }
  setMood(m) { if (MOODS[m]) this.mood = m; }
  setLevel(isNova, v) { if (isNova) this.nova = clamp(v, 0, 1); else this.driver = clamp(v, 0, 1); }
  start() { if (!this.raf) { this.last = performance.now(); this.raf = requestAnimationFrame((n) => this.tick(n)); } }
  stop() { cancelAnimationFrame(this.raf); this.raf = 0; }
  tick(now) {
    this.raf = requestAnimationFrame((n) => this.tick(n));
    if (this.cv.offsetParent === null) return;   // hidden page: skip drawing
    this.draw(this.step(now), now);
  }
  step(now) {
    const dt = Math.max(0, now - this.last); this.last = now;
    const k = this.reduce ? 1 : dt > 0 ? 1 - Math.exp(-dt / TAU_MS) : 0;
    const tgt = targetPose(this.state, this.mood), p = this.pose;
    for (const key of Object.keys(p)) {
      if (key === 'hue') { p.hue = (p.hue + hueDiff(p.hue, tgt.hue) * k + 360) % 360; }
      else p[key] += (tgt[key] - p[key]) * k;
    }
    this.scan += ((this.state === ST.THINKING ? 1 : 0) - this.scan) * k;
    const t = ((now - this.t0) % 600000) / 1000;
    const f = { p: { ...p }, swell: 1, nod: 1, hop: 0 };
    if (this.reduce) return f;
    if (this.state === ST.LISTENING) f.swell = 1 + 0.025 * (this.driver > 0 ? this.driver : envelope(t * 0.85, 2.1));
    if (this.state === ST.SPEAKING) {
      const n = this.nova > 0 ? this.nova : envelope(t, 0.3);
      f.p.eyeH *= 1 - 0.12 * n; f.nod = 1 - 0.02 * n;
    }
    f.p.lookX += 0.08 * Math.sin(0.9 * t) * this.scan;
    f.hop = 0.03 * p.bob * Math.sin(t * 1.4);
    if (now >= this.nextBlink && !this.blinking) {
      if (this.state === ST.MUTED) this.nextBlink = now + 3000 + Math.random() * 3000;
      else { this.blinking = true; this.blinkAt = this.nextBlink; }
    }
    if (this.blinking) {
      const q = (now - this.blinkAt) / BLINK_MS;
      if (q >= 1) { this.blinking = false; this.nextBlink = now + 3000 + Math.random() * 3000; }
      else f.p.lidT = Math.max(f.p.lidT, Math.sin(Math.PI * q));
    }
    f.p.lidT = clamp(f.p.lidT, 0, 1);
    return f;
  }
  draw(f, now) {
    const cv = this.cv, dpr = window.devicePixelRatio || 1;
    const css = Math.max(120, Math.round(cv.clientWidth));
    if (cv.width !== Math.round(css * dpr)) { cv.width = Math.round(css * dpr); cv.height = cv.width; }
    const g = cv.getContext('2d');
    g.setTransform(cv.width / css, 0, 0, cv.width / css, 0, 0);
    g.clearRect(0, 0, css, css);
    const p = f.p, s = css, R = 0.27 * s, cx = s / 2, cy = 0.53 * s + f.hop * R;
    const hsl = (l, a = 1) => `hsla(${p.hue.toFixed(1)}, ${clamp(p.sat, 0, 100).toFixed(1)}%, ${l}%, ${a})`;
    // soft glow behind the head
    const glow = g.createRadialGradient(cx, cy, R * 0.6, cx, cy, R * 1.8);
    glow.addColorStop(0, hsl(58, 0.22)); glow.addColorStop(1, hsl(58, 0));
    g.fillStyle = glow; g.fillRect(0, 0, s, s);
    // body (pebble): rounded rect seated at the bottom centre, swell/nod resize it about the seat
    const hw = 0.93, hh = 0.90, kx = f.swell, ky = f.swell * f.nod, seat = cy + R;
    const w = 2 * hw * R * kx, hgt = 2 * hh * R * ky, pivy = (1 + hh) * R * ky;
    g.save();
    g.translate(cx, seat); g.rotate(p.lean); g.translate(-cx, -seat);
    const bx = cx - hw * R * kx, by = seat - pivy;
    const grad = g.createLinearGradient(0, by, 0, by + hgt);
    grad.addColorStop(0, hsl(64)); grad.addColorStop(1, hsl(52));
    g.fillStyle = grad;
    g.beginPath(); g.roundRect(bx, by, w, hgt, Math.min(0.78 * R, w / 2, hgt / 2)); g.fill();
    g.restore();
    // eyes
    const headD = EYE_SCALE * R, mo = eyeMorph(p.eyeH, p.happy);
    const sx = f.swell, sy = f.swell * f.nod, cs = Math.cos(p.lean), sn = Math.sin(p.lean), px = cx, py = cy + R;
    for (let i = 0; i < 2; i++) {
      const side = i ? 1 : -1, scale = i ? 1 + p.asym * 0.35 : 1 - p.asym * 0.45;
      const ex0 = cx + (side * GAP + p.lookX) * R, ey0 = cy + p.lookY * R;
      const dx = (ex0 - px) * sx, dy = (ey0 - py) * sy;
      const ex = px + dx * cs - dy * sn, ey = py + dx * sn + dy * cs;
      const sc = scale * sx, ew = p.eyeW * headD * sc, eh = p.eyeH * headD * sc, hh2 = mo.pillH * headD * sc;
      const top = ey - hh2 / 2 + p.lidT * hh2, bot = ey + hh2 / 2 - p.lidB * hh2;
      const vh = Math.max(bot - top, 0.035 * R), ecy = (top + bot) / 2;
      g.save();
      g.translate(ex, ecy); g.rotate(p.lean + -side * p.tilt);
      g.fillStyle = '#fbfdff'; g.strokeStyle = '#fbfdff';
      if (mo.pillOpa > 0) {
        g.globalAlpha = mo.pillOpa;
        g.beginPath(); g.roundRect(-ew / 2, -vh / 2, ew, vh, Math.min(ew, vh) / 2); g.fill();
      }
      if (mo.archOpa > 0) {
        const rad = 0.62 * ew, lw = Math.max(1, 0.48 * ew * (0.55 + 0.45 * mo.archRise));
        const half = (61.2 * (0.4 + 0.6 * mo.archRise) * Math.PI) / 180;
        g.globalAlpha = mo.archOpa; g.lineWidth = lw; g.lineCap = 'round';
        g.beginPath(); g.arc(0, ey + 0.2 * eh - ecy, rad, -Math.PI / 2 - half, -Math.PI / 2 + half); g.stroke();
      }
      g.restore();
    }
    this.frameCount++;
  }
}
