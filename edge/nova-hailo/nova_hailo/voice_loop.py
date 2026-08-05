"""Self-contained voice loop with solid WM8960 capture for Whisper."""
from __future__ import annotations

import select
import sys
import termios
import time
import tty
from collections.abc import Callable

import numpy as np
import sounddevice as sd

TARGET_SR = 16000
MIN_SEC = 0.6
MIN_RMS = 0.008
SPACE_DEBOUNCE_S = 0.35


def _getch(timeout: float | None = None) -> str | None:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        if timeout is not None:
            r, _, _ = select.select([sys.stdin], [], [], timeout)
            if not r:
                return None
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            # swallow extra escape bytes if any
            if select.select([sys.stdin], [], [], 0.01)[0]:
                ch += sys.stdin.read(2)
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _resample(audio: np.ndarray, src_sr: float, dst_sr: float) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if abs(src_sr - dst_sr) < 1.0:
        return audio
    n_dst = int(len(audio) * dst_sr / src_sr)
    if n_dst <= 1 or len(audio) < 2:
        return np.array([], dtype=np.float32)
    x_old = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_dst, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def prepare_for_whisper(audio: np.ndarray, src_sr: float = TARGET_SR) -> tuple[np.ndarray, dict]:
    """Mono float32 16kHz, DC-removed, peak-normalized, truncated silence stripped."""
    info: dict = {}
    x = np.asarray(audio, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=-1)
    x = x.reshape(-1)
    if src_sr != TARGET_SR:
        x = _resample(x, src_sr, TARGET_SR)
    if len(x) == 0:
        return x, {"rms": 0.0, "peak": 0.0, "sec": 0.0}

    x = x - float(np.mean(x))
    peak = float(np.max(np.abs(x)) + 1e-9)
    rms = float(np.sqrt(np.mean(x * x)) + 1e-12)
    info["peak"] = peak
    info["rms"] = rms
    info["sec"] = len(x) / TARGET_SR

    # Soft peak normalize toward ~0.7 (Whisper likes decent level, not clipped)
    if peak > 1e-4:
        target = 0.7
        x = x * (target / peak)
        x = np.clip(x, -1.0, 1.0)

    # Drop leading/trailing near-silence (simple energy gate)
    win = max(1, int(0.02 * TARGET_SR))
    energy = np.convolve(x * x, np.ones(win) / win, mode="same")
    thr = max(1e-5, 0.15 * float(np.median(energy) + 1e-12) * 5)
    mask = energy > thr
    if np.any(mask):
        idx = np.where(mask)[0]
        pad = int(0.05 * TARGET_SR)
        a = max(0, int(idx[0]) - pad)
        b = min(len(x), int(idx[-1]) + pad)
        x = x[a:b]
        info["trimmed_sec"] = len(x) / TARGET_SR
    else:
        info["trimmed_sec"] = 0.0

    info["rms_after"] = float(np.sqrt(np.mean(x * x)) + 1e-12) if len(x) else 0.0
    return x.astype(np.float32), info


class SimpleVoiceLoop:
    """SPACE to toggle record; q quit; c clear; x abort."""

    def __init__(
        self,
        on_audio: Callable[[np.ndarray], None],
        on_start: Callable[[], None] | None = None,
        on_clear: Callable[[], None] | None = None,
        on_abort: Callable[[], None] | None = None,
        on_shutdown: Callable[[], None] | None = None,
        device_id: int | None = None,
        title: str = "Nova-Hailo",
    ):
        self.on_audio = on_audio
        self.on_start = on_start
        self.on_clear = on_clear
        self.on_abort = on_abort
        self.on_shutdown = on_shutdown
        self.device_id = device_id
        self.title = title
        self._frames: list[np.ndarray] = []
        self._recording = False
        self._stream: sd.InputStream | None = None
        self._device_sr = 48000.0
        self._channels = 1
        self._last_space_t = 0.0
        self._rec_t0 = 0.0
        if device_id is not None:
            try:
                info = sd.query_devices(device_id)
                self._device_sr = float(info.get("default_samplerate") or 48000)
                # WM8960 is stereo; capture 2ch and downmix (more reliable than mono)
                max_in = int(info.get("max_input_channels") or 1)
                self._channels = 2 if max_in >= 2 else 1
            except Exception:
                self._device_sr = 48000.0
                self._channels = 2

    def _callback(self, indata, frames, time_info, status):
        if not self._recording:
            return
        # indata shape (N, C)
        self._frames.append(indata.copy())

    def _start_rec(self):
        if self.on_start:
            self.on_start()
        self._frames = []
        self._recording = True
        self._rec_t0 = time.monotonic()
        tried = [
            (self._device_sr, self._channels),
            (48000.0, 2),
            (44100.0, 2),
            (16000.0, 1),
        ]
        last_err = None
        for sr, ch in tried:
            try:
                self._stream = sd.InputStream(
                    samplerate=sr,
                    blocksize=max(256, int(0.03 * sr)),
                    device=self.device_id,
                    channels=ch,
                    dtype="float32",
                    callback=self._callback,
                )
                self._stream.start()
                self._device_sr = sr
                self._channels = ch
                print(f"● Recording... SPACE to stop  [{int(sr)} Hz, {ch}ch, dev={self.device_id}]")
                return
            except Exception as e:
                last_err = e
                self._stream = None
        self._recording = False
        print(f"Record failed: {last_err}")

    def _stop_rec(self) -> tuple[np.ndarray, dict]:
        self._recording = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if not self._frames:
            return np.array([], dtype=np.float32), {"sec": 0.0, "rms": 0.0}

        raw = np.concatenate(self._frames, axis=0).astype(np.float32)
        if raw.ndim == 2:
            mono = raw.mean(axis=1)
        else:
            mono = raw.reshape(-1)

        audio, info = prepare_for_whisper(mono, src_sr=self._device_sr)
        return audio, info

    def run(self):
        print(f"\n=== {self.title} ===")
        print("Hold SPACE: press once to START, once to STOP (wait ≥0.6s while talking)")
        print("c clear | x abort | q quit\n")
        try:
            while True:
                ch = _getch()
                if ch is None:
                    continue
                if ch in ("q", "Q", "\x03"):
                    break
                if ch in ("c", "C"):
                    if self.on_clear:
                        self.on_clear()
                    continue
                if ch in ("x", "X"):
                    if self.on_abort:
                        self.on_abort()
                    print("Aborted.")
                    continue
                if ch == " ":
                    now = time.monotonic()
                    if now - self._last_space_t < SPACE_DEBOUNCE_S:
                        continue
                    self._last_space_t = now

                    if not self._recording:
                        self._start_rec()
                    else:
                        # enforce min record duration
                        if time.monotonic() - self._rec_t0 < MIN_SEC:
                            print(f"Keep talking… (need ≥{MIN_SEC}s)")
                            continue
                        audio, info = self._stop_rec()
                        sec = info.get("sec", 0.0) or (len(audio) / TARGET_SR if len(audio) else 0)
                        rms = info.get("rms", 0.0)
                        print(
                            f"Captured {sec:.1f}s  rms={rms:.4f}  "
                            f"peak={info.get('peak', 0):.3f}  trimmed={info.get('trimmed_sec', 0):.1f}s"
                        )
                        if sec < MIN_SEC:
                            print("Too short, try again (speak longer).")
                            continue
                        if rms < MIN_RMS:
                            print(
                                "Mic level too low / silence — raise Capture in alsamixer -c 2, "
                                "speak closer. Skipping Whisper."
                            )
                            continue
                        print("Processing…")
                        self.on_audio(audio)
                        # prevent immediate restart from key repeat
                        self._last_space_t = time.monotonic()
        except KeyboardInterrupt:
            pass
        finally:
            if self._recording:
                self._stop_rec()
            if self.on_shutdown:
                self.on_shutdown()
