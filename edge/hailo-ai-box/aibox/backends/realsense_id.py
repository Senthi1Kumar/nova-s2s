"""RealSense ID on-device enrollment, authentication, and template management."""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
from pathlib import Path

_USER_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,29}$")


def validate_user_id(user_id: str) -> str:
    value = (user_id or "").strip()
    if not _USER_ID.fullmatch(value):
        raise ValueError("user_id must start with a letter and use letters, digits, _ or - (max 30)")
    return value


def status_name(status) -> str:
    return str(status).rsplit(".", 1)[-1]


class RealSenseFaceAuth:
    def __init__(self, lib_dir: str) -> None:
        path = Path(lib_dir)
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))
        try:
            import rsid_py
        except ImportError as exc:
            raise RuntimeError(f"RealSense ID SDK not available at {lib_dir}") from exc
        self._rsid = rsid_py
        self._lock = threading.Lock()

    def _device(self):
        devices = self._rsid.discover_devices()
        if not devices:
            raise RuntimeError("realsense_id_unavailable")
        return devices[0]

    def _connect(self):
        dev = self._device()
        return self._rsid.FaceAuthenticator(dev.device_type, dev.serial_port)

    async def users(self) -> list[str]:
        def run():
            with self._lock, self._connect() as auth:
                return list(auth.query_user_ids())

        return await asyncio.to_thread(run)

    async def enroll(self, user_id: str) -> dict:
        user_id = validate_user_id(user_id)

        def run():
            result = {"status": "Error", "faces": []}
            hints = []

            def on_result(status):
                result["status"] = status_name(status)

            def on_hint(status, score):
                hints.append({"status": status_name(status), "score": round(float(score), 3)})

            def on_faces(faces, _timestamp):
                result["faces"] = [{"x": f.x, "y": f.y, "w": f.w, "h": f.h} for f in faces]

            t0 = time.perf_counter()
            with self._lock, self._connect() as auth:
                auth.enroll(user_id=user_id, on_result=on_result, on_hint=on_hint, on_faces=on_faces)
            return {
                "enrolled": result["status"] == "Success",
                "user_id": user_id if result["status"] == "Success" else None,
                "status": result["status"],
                "method": "realsense_id",
                "device": "f455",
                "faces": result["faces"],
                "frame_width": 1080,
                "frame_height": 1920,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "hints": hints[-20:],
            }

        return await asyncio.to_thread(run)

    async def authenticate(self) -> dict:
        def run():
            result = {"status": "Error", "user_id": None, "faces": []}
            hints = []

            def on_result(status, user_id):
                result["status"] = status_name(status)
                result["user_id"] = user_id or None

            def on_hint(status, score):
                hints.append({"status": status_name(status), "score": round(float(score), 3)})

            def on_faces(faces, _timestamp):
                result["faces"] = [{"x": f.x, "y": f.y, "w": f.w, "h": f.h} for f in faces]

            t0 = time.perf_counter()
            with self._lock, self._connect() as auth:
                config = auth.query_device_config()
                auth.authenticate(on_result=on_result, on_hint=on_hint, on_faces=on_faces)
            return {
                "authenticated": result["status"] == "Success",
                "user_id": result["user_id"] if result["status"] == "Success" else None,
                "status": result["status"],
                "method": "realsense_id",
                "device": "f455",
                "security_level": str(config.security_level),
                "algo_flow": str(config.algo_flow),
                "face_selection_policy": str(config.face_selection_policy),
                "faces": result["faces"],
                "frame_width": 1080,
                "frame_height": 1920,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "hints": hints[-20:],
            }

        return await asyncio.to_thread(run)

    async def remove_user(self, user_id: str) -> dict:
        user_id = validate_user_id(user_id)

        def run():
            with self._lock, self._connect() as auth:
                auth.remove_user(user_id)
                remaining = list(auth.query_user_ids())
            return {"deleted": user_id not in remaining, "user_id": user_id,
                    "method": "realsense_id"}

        return await asyncio.to_thread(run)
