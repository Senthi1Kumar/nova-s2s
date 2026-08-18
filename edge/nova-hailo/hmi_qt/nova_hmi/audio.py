"""Duplex audio for HMI: mic uplink (PCM16 16 kHz) + TTS playback + orb bands."""
from __future__ import annotations

import math
import queue
import threading
import time
from collections import deque

from PySide6.QtCore import QObject, QTimer, Signal

BANDS = 24
CAPTURE_RATE = 16000
# Capture at 48 kHz (PipeWire/Pulse default) and downsample — requesting 16 kHz
# often yields a silent or broken stream while the orb still looks alive.
DEVICE_RATE = 48000
DEVICE_BLOCK = 1920  # 40 ms at 48 kHz → 640 samples at 16 kHz

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

try:
    import sounddevice as sd
except Exception:  # pragma: no cover
    sd = None


def _resample_int16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if not pcm or src_rate == dst_rate or np is None:
        return pcm
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if x.size < 2:
        return pcm
    n_out = max(1, int(round(x.size * dst_rate / float(src_rate))))
    t = np.linspace(0.0, 1.0, n_out, endpoint=False)
    idx = t * (x.size - 1)
    lo = np.floor(idx).astype(np.int32)
    hi = np.minimum(lo + 1, x.size - 1)
    frac = idx - lo
    y = x[lo] * (1.0 - frac) + x[hi] * frac
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


class AudioDuplex(QObject):
    """Viz bands + PCM uplink + playback queue."""

    frame = Signal(float, list)
    pcm16Ready = Signal(bytes)
    playbackStarted = Signal()
    playbackQueueEmpty = Signal()

    def __init__(self, parent=None, *, capture: bool = True, playback: bool = True):
        super().__init__(parent)
        self._level = 0.0
        self._bands = [0.0] * BANDS
        self._muted = False
        self._in = None
        self._out = None
        self._synthetic = True
        self._play_q: deque[bytes] = deque()
        self._play_lock = threading.Lock()
        self._play_rate = CAPTURE_RATE
        self._started_play = False
        self._play_was_active = False
        self._uplink: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._batch = bytearray()
        self._batch_target = int(CAPTURE_RATE * 0.06) * 2  # ~60 ms PCM16
        self._agc = 6.0
        self._rms_log_n = 0
        self._hold_until = 0.0
        self._echo_tail_s = 1.25
        self._last_play_activity = 0.0

        if capture and sd is not None and np is not None:
            try:
                dev = sd.query_devices(kind="input")
                ch = int(dev.get("max_input_channels") or 1)
                print(
                    f"[hmi] mic device: {dev.get('name')!r} "
                    f"in_ch={ch} default_sr={dev.get('default_samplerate')}",
                    flush=True,
                )
                self._in = sd.InputStream(
                    samplerate=DEVICE_RATE,
                    blocksize=DEVICE_BLOCK,
                    channels=max(1, min(ch, 2)),
                    dtype="float32",
                    callback=self._on_in,
                )
                self._in.start()
                self._synthetic = False
                print("[hmi] mic: 48 kHz capture → 16 kHz uplink + AGC", flush=True)
            except Exception as exc:
                self._in = None
                print(f"[hmi] mic FAILED ({exc}) — no uplink", flush=True)
        elif capture:
            print("[hmi] mic FAILED (need numpy+sounddevice) — no uplink", flush=True)

        if playback and sd is not None and np is not None:
            try:
                self._out = sd.OutputStream(
                    samplerate=self._play_rate,
                    blocksize=1024,
                    channels=1,
                    dtype="int16",
                    callback=self._on_out,
                )
                self._out.start()
                print("[hmi] speaker: playback open", flush=True)
            except Exception as exc:
                self._out = None
                print(f"[hmi] speaker FAILED ({exc})", flush=True)

        self._tick = QTimer(self)
        self._tick.setInterval(16)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()
        self._drain = QTimer(self)
        self._drain.setInterval(20)
        self._drain.timeout.connect(self._drain_uplink)
        self._drain.start()

    @property
    def synthetic(self) -> bool:
        return self._synthetic

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        if muted:
            self._batch.clear()

    def hold_uplink(self, seconds: float) -> None:
        """Drop mic uplink for a while (TTS playback + speaker→mic echo)."""
        self._hold_until = max(self._hold_until, time.monotonic() + max(0.0, seconds))
        self._batch.clear()

    def uplink_blocked(self) -> bool:
        if self._muted:
            return True
        if self.play_queue_depth() > 0:
            return True
        return time.monotonic() < self._hold_until

    def set_activity(self, _gain: float) -> None:
        return

    def play_queue_depth(self) -> int:
        with self._play_lock:
            return len(self._play_q)

    def enqueue_playback(self, pcm16: bytes, sample_rate: int = 24000) -> None:
        if not pcm16:
            return
        src_rate = int(sample_rate or 24000)
        chunk = _resample_int16(pcm16, src_rate, self._play_rate)
        dur = max(0.05, len(chunk) / 2.0 / float(self._play_rate))
        with self._play_lock:
            empty = not self._play_q
            self._play_q.append(chunk)
        self._last_play_activity = time.monotonic()
        # Hold only for this audio + echo tail — never a sticky "playing" flag.
        self.hold_uplink(dur + self._echo_tail_s)
        if empty and not self._started_play:
            self._started_play = True
            self._play_was_active = True
            self.playbackStarted.emit()
            if self._out is None:
                print("[hmi] no speaker device — not parking the mic", flush=True)
                self._started_play = False

    def flush_playback(self) -> None:
        with self._play_lock:
            self._play_q.clear()
        self._started_play = False
        self.hold_uplink(self._echo_tail_s)

    def _on_in(self, indata, frames, time_info, status):  # PortAudio thread
        if np is None:
            return
        raw = np.asarray(indata, dtype=np.float32)
        mono48 = raw.mean(axis=1) if raw.ndim == 2 and raw.shape[1] > 1 else raw.reshape(-1)
        # Integer-ratio 48k → 16k (every 3rd sample after a 3-tap average).
        if mono48.size >= 3:
            n = mono48.size // 3
            a = mono48[: n * 3].reshape(n, 3).mean(axis=1)
        else:
            a = mono48
        rms = float(np.sqrt(np.mean(a**2)) + 1e-12)
        blocked = self.uplink_blocked()
        # Slow AGC: laptop mics sit ~0.005 RMS; Pi gate wants ≥ 0.016.
        # Freeze AGC while the speaker is up so TTS does not train the gain.
        if not blocked:
            target = 0.05
            if rms > 1e-4:
                desired = target / rms
                self._agc = 0.90 * self._agc + 0.10 * float(min(20.0, max(1.0, desired)))
        a = np.clip(a * self._agc, -1.0, 1.0)
        out_rms = float(np.sqrt(np.mean(a**2)) + 1e-12)
        self._rms_log_n += 1
        if self._rms_log_n % 25 == 1:
            print(
                f"[hmi] uplink rms_in={rms:.4f} agc={self._agc:.1f} "
                f"rms_out={out_rms:.4f} blocked={blocked}",
                flush=True,
            )
        db = 20 * math.log10(max(out_rms, 1e-6))
        self._level = max(0.0, min(1.0, (db + 55.0) / 55.0))
        nfft = 1 << int(math.ceil(math.log2(max(len(a), 2))))
        padded = np.zeros(nfft, dtype=np.float32)
        padded[: len(a)] = a * np.hanning(len(a))
        mag = np.abs(np.fft.rfft(padded))
        freqs = np.fft.rfftfreq(nfft, 1.0 / CAPTURE_RATE)
        edges = np.logspace(math.log10(80), math.log10(6000), BANDS + 1)
        out = []
        for i in range(BANDS):
            lo = int(np.searchsorted(freqs, edges[i]))
            hi = max(int(np.searchsorted(freqs, edges[i + 1])), lo + 1)
            seg = mag[lo:hi]
            e = float(seg.mean()) if seg.size else 0.0
            out.append(max(0.0, min(1.0, math.log10(1 + e * 60) / 1.6)))
        self._bands = out
        if blocked or self._muted:
            self._batch.clear()
            return
        i16 = np.clip(a * 32767.0, -32768, 32767).astype(np.int16).tobytes()
        self._batch.extend(i16)
        while len(self._batch) >= self._batch_target:
            chunk = bytes(self._batch[: self._batch_target])
            del self._batch[: self._batch_target]
            try:
                self._uplink.put_nowait(chunk)
            except queue.Full:
                break

    def _on_out(self, outdata, frames, time_info, status):  # PortAudio thread
        need = frames * 2
        buf = bytearray()
        with self._play_lock:
            while len(buf) < need and self._play_q:
                piece = self._play_q.popleft()
                buf.extend(piece)
            extra = bytes(buf[need:])
            if extra:
                self._play_q.appendleft(extra)
            drained = not self._play_q and len(buf) <= need
        if len(buf) < need:
            buf.extend(b"\x00" * (need - len(buf)))
        outdata[:] = np.frombuffer(bytes(buf[:need]), dtype=np.int16).reshape(-1, 1)
        if drained and self._started_play:
            self._started_play = False
        if len(buf) >= 2:
            self._last_play_activity = time.monotonic()

    def _on_tick(self) -> None:
        self.frame.emit(self._level, list(self._bands))
        # Stale queue (device never pulls) must not mute the mic forever.
        if (
            self.play_queue_depth() > 0
            and self._last_play_activity
            and time.monotonic() - self._last_play_activity > 3.0
        ):
            print("[hmi] playback stalled — flush and reopen mic", flush=True)
            self.flush_playback()
        if not self._started_play and self._play_was_active:
            self._play_was_active = False
            self.hold_uplink(self._echo_tail_s)
            print(f"[hmi] playback done — mute uplink {self._echo_tail_s:.2f}s", flush=True)
            self.playbackQueueEmpty.emit()
        elif self._started_play:
            self._play_was_active = True

    def _drain_uplink(self) -> None:
        while True:
            try:
                chunk = self._uplink.get_nowait()
            except queue.Empty:
                return
            self.pcm16Ready.emit(chunk)

    def stop(self) -> None:
        self._tick.stop()
        self._drain.stop()
        self.flush_playback()
        for stream in (self._in, self._out):
            if stream is None:
                continue
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self._in = self._out = None
