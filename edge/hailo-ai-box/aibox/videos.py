"""Video sources for CV tasks: preset directory + uploads, probing and looping playback."""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

EXTENSIONS = (".mp4", ".webm", ".mov", ".mkv")
MAX_VIDEO_BYTES = 200 * 1024 * 1024
UPLOAD_MAX_AGE_S = 24 * 3600
_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")


class VideoError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def probe(path: str) -> dict | None:
    """duration_s / width / height / fps of a decodable video, else None."""
    import cv2

    cap = cv2.VideoCapture(path)
    try:
        if not cap.isOpened():
            return None
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if fps <= 0 or frames <= 0 or w <= 0 or h <= 0:
            return None
        return {"duration_s": round(frames / fps, 1), "width": w, "height": h,
                "fps": round(fps, 2)}
    finally:
        cap.release()


class VideoLibrary:
    def __init__(self, preset_dir: str, upload_dir: str) -> None:
        self.preset_dir, self.upload_dir = preset_dir, upload_dir
        self._cache: dict[tuple[str, float], dict | None] = {}
        self._lock = threading.Lock()

    def _probe(self, path: str) -> dict | None:
        try:
            key = (path, os.path.getmtime(path))
        except OSError:
            return None
        with self._lock:
            if key not in self._cache:
                self._cache[key] = probe(path)
            return self._cache[key]

    def _scan(self, directory: str, source: str, prefix: str = "") -> list[dict]:
        out = []
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return out
        for fn in names:
            stem, ext = os.path.splitext(fn)
            path = os.path.join(directory, fn)
            if ext.lower() not in EXTENSIONS or not os.path.exists(path):  # dangling symlink
                continue
            info = self._probe(path)
            if info is None:
                continue
            out.append({"id": prefix + stem, "name": stem, "source": source, "path": path, **info})
        return out

    def list(self) -> list[dict]:
        self.prune()
        return self._scan(self.preset_dir, "preset") + self._scan(self.upload_dir, "upload")

    def get(self, video_id: str) -> dict | None:
        return next((v for v in self.list() if v["id"] == video_id), None)

    @staticmethod
    def public(item: dict) -> dict:
        return {k: item[k] for k in ("id", "name", "duration_s", "width", "height", "fps", "source")}

    def prune(self, now: float | None = None) -> int:
        """Delete uploads older than 24 h."""
        now = time.time() if now is None else now
        n = 0
        try:
            names = os.listdir(self.upload_dir)
        except OSError:
            return 0
        for fn in names:
            path = os.path.join(self.upload_dir, fn)
            try:
                if os.path.isfile(path) and now - os.path.getmtime(path) > UPLOAD_MAX_AGE_S:
                    os.remove(path)
                    n += 1
            except OSError:
                pass
        return n

    def add_upload(self, tmp_path: str, filename: str) -> dict:
        """Validate a spooled upload, then store it under upload_dir and return the item."""
        ext = os.path.splitext(filename)[1].lower()
        if ext not in EXTENSIONS:
            raise VideoError(415, f"unsupported video type '{ext or filename}'; use mp4, webm, mov or mkv")
        os.makedirs(self.upload_dir, exist_ok=True)
        stem = _ID_RE.sub("_", os.path.splitext(os.path.basename(filename))[0])[:40].strip("_") or "video"
        vid = f"up_{uuid.uuid4().hex[:6]}_{stem}"
        dest = os.path.join(self.upload_dir, vid + ext)
        os.replace(tmp_path, dest)
        info = probe(dest)
        if info is None:
            os.remove(dest)
            raise VideoError(415, "could not decode this video")
        return {"id": vid, "name": stem, "source": "upload", "path": dest, **info}


async def play(path: str, fps: float, *, loop: bool = True) -> AsyncIterator[tuple[np.ndarray, float]]:
    """Yield (RGB frame, video_t seconds) paced at min(fps, video fps), looping at the end."""
    import cv2

    cap = await asyncio.to_thread(cv2.VideoCapture, path)
    try:
        if not cap.isOpened():
            raise RuntimeError(f"could not open video {os.path.basename(path)}")
        vfps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        eff = max(0.1, min(float(fps), vfps))
        step = max(1, round(vfps / eff))
        period = 1.0 / eff
        nxt = time.monotonic()
        idx = 0
        failed = 0
        while True:
            ok, bgr = await asyncio.to_thread(cap.read)
            if not ok or bgr is None:
                if not loop or failed:
                    if failed:
                        raise RuntimeError("video has no decodable frames")
                    return
                failed += 1
                await asyncio.to_thread(cap.set, cv2.CAP_PROP_POS_FRAMES, 0)
                idx = 0
                continue
            failed = 0
            t = idx / vfps
            yield cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), round(t, 2)
            for _ in range(step - 1):  # skip to the requested rate
                await asyncio.to_thread(cap.grab)
            idx += step
            nxt = max(nxt + period, time.monotonic())
            await asyncio.sleep(max(0.0, nxt - time.monotonic()))
    finally:
        await asyncio.to_thread(cap.release)


def library_for(app_state: Any, settings) -> VideoLibrary:
    lib = getattr(app_state, "videos", None)
    if lib is None:
        lib = VideoLibrary(settings.video_dir, settings.upload_dir)
        app_state.videos = lib
    return lib
