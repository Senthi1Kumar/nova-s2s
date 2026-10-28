"""Shared OpenCV capture: one thread per source, latest frame wins, closed with the last subscriber."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import numpy as np

from ..config import Settings
from ..services import CameraService

# per-source capture requests: (fourcc, width, height)
CAPTURE_MODES = {"c920e": ("MJPG", 640, 360), "f455": ("YUYV", 352, 640)}
MAX_READ_FAILURES = 30
REOPEN_WAIT_S = 3.0


def open_v4l2(name: str, path: str) -> Any:
    import cv2

    cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
    mode = CAPTURE_MODES.get(name)
    if mode:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*mode[0]))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, mode[1])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, mode[2])
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


class _Source:
    def __init__(self, name: str, capture: Any) -> None:
        self.name, self.capture = name, capture
        self.subscribers = 0
        self.seq = 0
        self.frame: np.ndarray | None = None
        self.error: str | None = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name=f"cam-{name}", daemon=True)

    def _loop(self) -> None:
        import cv2

        fails = 0
        try:
            while not self.stop.is_set():
                ok, bgr = self.capture.read()
                if not ok or bgr is None:
                    fails += 1
                    if fails >= MAX_READ_FAILURES:
                        self.error = f"camera {self.name}: capture stopped returning frames"
                        return
                    time.sleep(0.05)
                    continue
                fails = 0
                self.frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                self.seq += 1
        finally:
            try:
                self.capture.release()
            except Exception:  # noqa: BLE001
                pass


class Cameras(CameraService):
    def __init__(self, settings: Settings,
                 opener: Callable[[str, str], Any] = open_v4l2) -> None:
        self._paths = dict(settings.cameras)
        self._opener = opener
        self._src: dict[str, _Source] = {}
        self._closing: dict[str, _Source] = {}
        self._lock = threading.Lock()

    def sources(self) -> list[str]:
        return list(self._paths)

    def active(self) -> dict[str, int]:
        with self._lock:
            return {n: s.subscribers for n, s in self._src.items()}

    def _open(self, name: str) -> Any:
        """Open the device; a just-closed stream's capture thread may still hold it (V4L2 is
        exclusive), so wait for it to exit and retry briefly instead of failing the new stream."""
        deadline = time.monotonic() + REOPEN_WAIT_S
        while True:
            with self._lock:
                old = self._closing.get(name)
            if old is not None and old.thread.is_alive():
                old.thread.join(max(0.05, deadline - time.monotonic()))
            cap = self._opener(name, self._paths[name])
            if cap.isOpened():
                return cap
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(f"camera {name} could not be opened ({self._paths[name]})")
            time.sleep(0.1)

    def _acquire(self, name: str) -> _Source:
        with self._lock:
            src = self._src.get(name)
            if src is not None:
                src.subscribers += 1
                return src
        cap = self._open(name)  # outside the lock: may wait for the previous stream to exit
        with self._lock:
            src = self._src.get(name)
            if src is None:
                src = _Source(name, cap)
                src.thread.start()
                self._src[name] = src
                cap = None
            src.subscribers += 1
        if cap is not None:  # lost a race with a concurrent subscriber
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass
        return src

    def _release(self, src: _Source) -> None:
        with self._lock:
            src.subscribers -= 1
            if src.subscribers <= 0:
                src.stop.set()
                if self._src.get(src.name) is src:
                    del self._src[src.name]
                self._closing[src.name] = src

    async def frames(self, source: str, fps: float) -> AsyncIterator[np.ndarray]:
        if source not in self._paths:
            raise KeyError(source)
        src = await asyncio.to_thread(self._acquire, source)
        try:
            last = -1
            period = 1.0 / max(fps, 0.1)
            nxt = time.monotonic()
            deadline = time.monotonic() + 5.0
            while True:
                if src.error:
                    raise RuntimeError(src.error)
                if src.seq != last and src.frame is not None:
                    last = src.seq
                    deadline = time.monotonic() + 5.0
                    yield src.frame.copy()  # writable, detached from the capture thread
                    nxt = max(nxt + period, time.monotonic())
                    await asyncio.sleep(max(0.0, nxt - time.monotonic()))
                else:
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"camera {source}: no frames for 5 s")
                    await asyncio.sleep(0.005)
        finally:
            self._release(src)
