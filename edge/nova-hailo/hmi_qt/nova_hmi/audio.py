"""Duplex audio for HMI: mic uplink (PCM16 16 kHz) + TTS playback + orb bands."""
from __future__ import annotations

import math
import queue
import threading
import time
from collections import deque

from PySide6.QtCore import QObject, QTimer, Signal

from nova_hmi.audio_gain import (
    AGC_ENABLED,
    HOT_RMS,
    MANUAL_GAIN,
    NOISE_FLOOR,
    capture_looks_dead,
    level_from_rms,
    pick_input_index,
    playback_done_debounce_s,
    should_send_uplink,
    uplink_multiplier,
)

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


def _input_device_index() -> int | None:
    if sd is None:
        return None
    try:
        default = sd.query_devices(kind="input")
        ch = int(default.get("max_input_channels") or 1)
        devices = list(sd.query_devices())
    except Exception:
        return None
    return pick_input_index(devices, ch)


def _resample_int16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Integer-ratio 16k/24k → 48 kHz (same as qt_mic_app). Generic lerp otherwise."""
    from nova_hmi.audio_gain import resample_int16

    return resample_int16(pcm, src_rate, dst_rate)


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
        self._play_rate = DEVICE_RATE
        self._started_play = False
        self._play_was_active = False
        self._uplink: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._batch = bytearray()
        self._batch_target = int(CAPTURE_RATE * 0.06) * 2  # ~60 ms PCM16
        self._agc = 1.0
        self._rms_log_n = 0
        self._hold_until = 0.0
        self._echo_tail_s = 0.55
        self._last_play_activity = 0.0
        self._last_in_rms = 0.0
        self._silent_s = 0.0
        self._last_mic_restart = 0.0
        self._xruns = 0
        self._viz_pcm: np.ndarray | None = None if np is None else np.zeros(640, dtype=np.float32)
        self._in_kw: dict = {}
        self._in_ch = 1
        self._restarting_in = False

        if capture and sd is not None and np is not None:
            try:
                dev_idx = _input_device_index()
                dev = sd.query_devices(dev_idx if dev_idx is not None else None, kind="input")
                ch = int(dev.get("max_input_channels") or 1)
                use_ch = 1 if "wm8960" in str(dev.get("name") or "").lower() else max(1, min(ch, 2))
                print(
                    f"[hmi] mic device: {dev.get('name')!r} idx={dev_idx} "
                    f"in_ch={ch} use_ch={use_ch} default_sr={dev.get('default_samplerate')}",
                    flush=True,
                )
                in_kw = {}
                if dev_idx is not None:
                    in_kw["device"] = dev_idx
                self._in_kw = in_kw
                self._in_ch = use_ch
                self._in = sd.InputStream(
                    samplerate=DEVICE_RATE,
                    blocksize=DEVICE_BLOCK,
                    channels=use_ch,
                    dtype="float32",
                    callback=self._on_in,
                    **in_kw,
                )
                self._in.start()
                self._synthetic = False
                print(
                    f"[hmi] mic: 48 kHz capture → 16 kHz uplink  "
                    f"gain={MANUAL_GAIN}× agc={'on' if AGC_ENABLED else 'off'}",
                    flush=True,
                )
            except Exception as exc:
                self._in = None
                print(f"[hmi] mic FAILED ({exc}) — no uplink", flush=True)
        elif capture:
            print("[hmi] mic FAILED (need numpy+sounddevice) — no uplink", flush=True)

        if playback and sd is not None and np is not None:
            try:
                # Keep Pulse/PipeWire default. Pinning ALSA wm8960 at 16 kHz
                # opens a silent stream (card wants 48 kHz); TTS then queues,
                # mic stays blocked, and the user only hears/gets mid-words.
                self._out = sd.OutputStream(
                    samplerate=self._play_rate,
                    blocksize=1024,
                    channels=1,
                    dtype="int16",
                    callback=self._on_out,
                )
                self._out.start()
                print(f"[hmi] speaker: playback {self._play_rate} Hz", flush=True)
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
        # Sticky while TTS is in the device — gaps between streamed clauses
        # used to reopen the mic and let the speaker feed Silero.
        if self._started_play:
            return True
        if self._out is not None and self.play_queue_depth() > 0:
            return True
        return time.monotonic() < self._hold_until

    def remaining_playback_s(self) -> float:
        with self._play_lock:
            n = sum(len(x) for x in self._play_q)
        return n / 2.0 / float(self._play_rate or DEVICE_RATE)

    def set_activity(self, _gain: float) -> None:
        return

    def play_queue_depth(self) -> int:
        with self._play_lock:
            return len(self._play_q)

    def enqueue_playback(self, pcm16: bytes, sample_rate: int = 24000) -> None:
        if not pcm16:
            return
        if self._out is None:
            print("[hmi] no speaker — drop TTS (mic stays open)", flush=True)
            return
        src_rate = int(sample_rate or 24000)
        chunk = _resample_int16(pcm16, src_rate, self._play_rate)
        dur = max(0.05, len(chunk) / 2.0 / float(self._play_rate))
        with self._play_lock:
            empty = not self._play_q
            self._play_q.append(chunk)
        self._last_play_activity = time.monotonic()
        self.hold_uplink(dur + self._echo_tail_s)
        if empty or not self._started_play:
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
        # Keep this cheap. FFT on this thread xruns the WM8960 under Piper/OR load,
        # after which PortAudio keeps calling us with zeros (orb dead, VAD idle,
        # backend still counts uplink frames).
        if np is None:
            return
        if status:
            self._xruns += 1
        raw = np.asarray(indata, dtype=np.float32)
        mono48 = raw.mean(axis=1) if raw.ndim == 2 and raw.shape[1] > 1 else raw.reshape(-1)
        if mono48.size >= 3:
            n = mono48.size // 3
            a = mono48[: n * 3].reshape(n, 3).mean(axis=1)
        else:
            a = mono48
        rms = float(np.sqrt(np.mean(a**2)) + 1e-12)
        self._last_in_rms = rms
        self._viz_pcm = np.array(a, dtype=np.float32, copy=True)
        blocked = self.uplink_blocked()
        if AGC_ENABLED and not blocked:
            if rms >= HOT_RMS:
                self._agc = 1.0
            elif rms > NOISE_FLOOR:
                desired = 0.04 / rms
                self._agc = 0.92 * self._agc + 0.08 * float(
                    min(8.0, max(1.0, desired))
                )
            else:
                self._agc = 0.95 * self._agc + 0.05 * 1.0
        self._level = level_from_rms(rms)
        gain = uplink_multiplier(rms=rms, agc=self._agc)
        boosted = np.clip(a * gain, -1.0, 1.0)
        if not should_send_uplink(rms=rms, blocked=blocked, muted=self._muted):
            self._batch = bytearray()
            return
        i16 = np.clip(boosted * 32767.0, -32768, 32767).astype(np.int16).tobytes()
        if len(self._batch) > self._batch_target * 4:
            self._batch = bytearray()
        self._batch.extend(i16)
        while len(self._batch) >= self._batch_target:
            chunk = bytes(self._batch[: self._batch_target])
            del self._batch[: self._batch_target]
            try:
                self._uplink.put_nowait(chunk)
            except queue.Full:
                try:
                    self._uplink.get_nowait()
                    self._uplink.put_nowait(chunk)
                except queue.Empty:
                    break
                except queue.Full:
                    break

    def _on_out(self, outdata, frames, time_info, status):  # PortAudio thread
        if status:
            self._xruns += 1
        need = frames * 2
        buf = bytearray()
        with self._play_lock:
            while len(buf) < need and self._play_q:
                piece = self._play_q.popleft()
                buf.extend(piece)
            extra = bytes(buf[need:])
            if extra:
                self._play_q.appendleft(extra)
            pulled = len(buf)
        if len(buf) < need:
            buf.extend(b"\x00" * (need - len(buf)))
        outdata[:] = np.frombuffer(bytes(buf[:need]), dtype=np.int16).reshape(-1, 1)
        if pulled >= 2:
            self._last_play_activity = time.monotonic()

    def _update_bands(self) -> None:
        if np is None:
            return
        a = self._viz_pcm
        if a is None or getattr(a, "size", 0) < 8:
            return
        nfft = 1 << int(math.ceil(math.log2(max(len(a), 2))))
        padded = np.zeros(nfft, dtype=np.float32)
        win = a * np.hanning(len(a))
        padded[: len(a)] = win
        mag = np.abs(np.fft.rfft(padded))
        freqs = np.fft.rfftfreq(nfft, 1.0 / CAPTURE_RATE)
        edges = np.logspace(math.log10(80), math.log10(6000), BANDS + 1)
        peak = float(np.max(mag) + 1e-9)
        out = []
        for i in range(BANDS):
            lo = int(np.searchsorted(freqs, edges[i]))
            hi = max(int(np.searchsorted(freqs, edges[i + 1])), lo + 1)
            seg = mag[lo:hi]
            e = float(seg.mean()) if seg.size else 0.0
            out.append(max(0.0, min(1.0, e / peak)))
        self._bands = out

    def _restart_input(self) -> None:
        if sd is None or np is None or self._restarting_in or self._in is None:
            return
        self._restarting_in = True
        try:
            if self._in is not None:
                try:
                    self._in.stop()
                    self._in.close()
                except Exception:
                    pass
                self._in = None
            self._in = sd.InputStream(
                samplerate=DEVICE_RATE,
                blocksize=DEVICE_BLOCK,
                channels=self._in_ch,
                dtype="float32",
                callback=self._on_in,
                **self._in_kw,
            )
            self._in.start()
            self._synthetic = False
            print("[hmi] mic InputStream restarted", flush=True)
        except Exception as exc:
            self._in = None
            print(f"[hmi] mic restart FAILED ({exc})", flush=True)
        finally:
            self._restarting_in = False

    def _on_tick(self) -> None:
        self._update_bands()
        self.frame.emit(self._level, list(self._bands))
        now = time.monotonic()
        idle_s = (now - self._last_play_activity) if self._last_play_activity else 9e9
        # Stale queue (device never pulls) must not mute the mic forever.
        if self.play_queue_depth() > 0 and self._last_play_activity and idle_s > 3.0:
            print("[hmi] playback stalled — flush and reopen mic", flush=True)
            self.flush_playback()
        if playback_done_debounce_s(
            queue_empty=self.play_queue_depth() == 0,
            started_play=self._started_play,
            idle_s=idle_s,
            hold_s=0.35,
        ):
            self._started_play = False
            self._play_was_active = False
            self.hold_uplink(self._echo_tail_s)
            print(f"[hmi] playback done — mute uplink {self._echo_tail_s:.2f}s", flush=True)
            self.playbackQueueEmpty.emit()
        elif self._started_play:
            self._play_was_active = True
        self._rms_log_n += 1
        if self._rms_log_n % 60 == 1:
            print(
                f"[hmi] uplink rms_in={self._last_in_rms:.4f} "
                f"blocked={self.uplink_blocked()} xruns={self._xruns} "
                f"play_q={self.play_queue_depth()}",
                flush=True,
            )
        # Zombie capture: callbacks still fire with digital zeros, not a quiet room.
        zombie = self._in is not None and capture_looks_dead(
            rms=self._last_in_rms,
            blocked=self.uplink_blocked(),
            threshold=1e-6,
        )
        if zombie:
            if self._silent_s <= 0.0:
                self._silent_s = now
            elif now - self._silent_s >= 4.0 and now - self._last_mic_restart > 15.0:
                print(
                    f"[hmi] mic silent rms={self._last_in_rms:.6f} — restarting capture",
                    flush=True,
                )
                self._silent_s = 0.0
                self._last_mic_restart = now
                self._restart_input()
        else:
            self._silent_s = 0.0

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
