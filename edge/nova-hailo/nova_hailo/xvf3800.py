"""reSpeaker XVF3800 status over USB (Seeed's xvf_host.py protocol, read-only).

Callers: web.realtime_session, scripts/xvf3800_probe.py. Needs pyusb and the udev rule from
scripts/setup_pi.sh; without them open() is False and nothing else changes.
"""
from __future__ import annotations

import math
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

VID, PID = 0x2886, 0x001A
_REQUEST_IN = 0xC0          # device-to-host | vendor | device
_DONE, _BUSY = 0, 64
_TIMEOUT_MS = 100
_MAX_TRIES = 20
# Polls share the control endpoint with the kernel's USB-audio clock requests: keep them slow.
POLL_HZ = 4.0

# name -> (resid, cmdid, count, struct code), from the XVF3800 v3.2.1 control command appendix.
COMMANDS: dict[str, tuple[int, int, int, str]] = {
    "AEC_SPENERGY_VALUES": (33, 80, 4, "f"),        # beam 1, beam 2, free-running, auto-select; > 0 is speech
    "AEC_AECCONVERGED": (33, 3, 1, "i"),
    "AEC_AECPATHCHANGE": (33, 0, 1, "i"),
    "AUDIO_MGR_SELECTED_AZIMUTHS": (35, 11, 2, "f"),  # processed DoA, auto-beam DoA; NaN without speech
    "VERSION": (48, 0, 3, "B"),
}


def read(dev: Any, name: str) -> tuple | None:
    """One command's values, or None when the board stays busy or the reply is short."""
    resid, cmdid, count, code = COMMANDS[name]
    size = struct.calcsize("<" + code)
    length = count * size + 1
    for _ in range(_MAX_TRIES):
        reply = bytes(dev.ctrl_transfer(_REQUEST_IN, 0, 0x80 | cmdid, resid, length, _TIMEOUT_MS))
        if reply and reply[0] == _BUSY:
            time.sleep(0.002)
            continue
        if not reply or reply[0] != _DONE or len(reply) < length:
            return None
        return struct.unpack("<" + code * count, reply[1:length])
    return None


@dataclass(frozen=True)
class BoardState:
    t: float
    energy: tuple[float, float, float, float]
    converged: bool
    path_change: bool
    azimuth: float | None = None     # radians; None when neither fixed beam holds speech

    @property
    def speech_energy(self) -> float:
        """The auto-select beam's speech energy: what the board would send as the voice."""
        return float(self.energy[3])

    @property
    def speech(self) -> bool:
        return self.speech_energy > 0.0


def poll(dev: Any, now: float, *, with_azimuth: bool = False) -> BoardState | None:
    energy = read(dev, "AEC_SPENERGY_VALUES")
    converged = read(dev, "AEC_AECCONVERGED")
    path = read(dev, "AEC_AECPATHCHANGE")
    if energy is None or converged is None or path is None:
        return None
    azimuth = None
    if with_azimuth:
        az = read(dev, "AUDIO_MGR_SELECTED_AZIMUTHS")
        if az is not None and not math.isnan(az[0]):
            azimuth = float(az[0])
    return BoardState(now, tuple(float(e) for e in energy), bool(converged[0]), bool(path[0]), azimuth)


def _find_usb() -> Any:
    import usb.core

    return usb.core.find(idVendor=VID, idProduct=PID)


class Xvf3800:
    """Polls the board on its own thread; latest() is safe from any thread."""

    def __init__(self, *, poll_hz: float = POLL_HZ, find: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._find = find or _find_usb
        self._clock = clock
        self._period = 1.0 / max(1.0, poll_hz)
        self._dev: Any = None
        self._state: BoardState | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._polls = 0
        self.error = ""
        self.version = ""

    def open(self) -> bool:
        try:
            dev = self._find()
            if dev is None:
                self.error = "no XVF3800 on USB"
                return False
            version = read(dev, "VERSION")
        except Exception as exc:   # ImportError (no pyusb), USBError / PermissionError (no udev rule)
            self.error = f"{type(exc).__name__}: {exc}"
            return False
        if version is None:
            self.error = "the board did not answer"
            return False
        self._dev = dev
        self.version = ".".join(str(v) for v in version)
        return True

    def poll_once(self) -> BoardState | None:
        if self._dev is None:
            return None
        self._polls += 1
        state = poll(self._dev, self._clock(), with_azimuth=self._polls % 4 == 0)
        if state is not None:
            with self._lock:
                self._state = state
        return state

    def latest(self, max_age_s: float = 1.0) -> BoardState | None:   # the flags change slowly
        with self._lock:
            state = self._state
        if state is None or self._clock() - state.t > max_age_s:
            return None
        return state

    def start(self) -> bool:
        if self._dev is None and not self.open():
            return False
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="xvf3800")
            self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                failures = 0 if self.poll_once() is not None else failures + 1
            except Exception as exc:   # unplugged, or the USB stack hiccuped
                failures += 1
                self.error = f"{type(exc).__name__}: {exc}"
            if failures >= 20:          # about a second of nothing: drop the handle and look again
                self._dev = None
                self._stop.wait(5.0)
                self.open()
                failures = 0
            self._stop.wait(max(0.0, self._period - (time.monotonic() - t0)))


_BOARD: Xvf3800 | None = None
_BOARD_TRIED = False
_BOARD_LOCK = threading.Lock()


def board() -> Xvf3800 | None:
    """The process's one reader, started on first use; None when the board cannot be read."""
    global _BOARD, _BOARD_TRIED
    with _BOARD_LOCK:
        if not _BOARD_TRIED:
            _BOARD_TRIED = True
            reader = Xvf3800()
            if reader.start():
                _BOARD = reader
                print(f"[xvf] reading the XVF3800 over USB (firmware {reader.version})", flush=True)
            else:
                print(f"[xvf] board status unavailable: {reader.error}", flush=True)
        return _BOARD
