"""Deterministic in-memory services for tests and AIBOX_FAKE=1."""

from __future__ import annotations

import asyncio
import io
import wave
from collections.abc import AsyncIterator
from typing import ClassVar

import numpy as np

from .config import Settings
from .inspector import Inspector
from .services import Services

TASKS = ["detection", "pose", "segmentation", "lanes", "dms", "ocr", "face_track"]
TASK_MODEL = {
    "detection": "yolov8m",
    "pose": "yolov8m_pose",
    "segmentation": "yolov5m_seg",
    "lanes": "ufld_v2_tu",
    "dms": "scrfd_10g",
    "ocr": "ocr",
}
FAKE_MODELS = [
    ("qwen3:1.7b", "chat", "hailo-ollama"),
    ("qwen2.5:1.5b", "chat", "hailo-ollama"),
    ("llama3.2:1b", "chat", "hailo-ollama"),
    ("qwen2:1.5b", "chat", "hailo-ollama"),
    ("qwen3-vl", "vlm", "hailort-genai"),
    ("qwen2-vl", "vlm", "hailort-genai"),
    ("whisper-base", "asr", "hailort"),
    ("piper-amy", "tts", "piper"),
    ("yolov8m", "vision", "hailort"),
    ("yolov8m_pose", "vision", "hailort"),
    ("yolov5m_seg", "vision", "hailort"),
    ("ufld_v2_tu", "vision", "hailort"),
    ("scrfd_10g", "vision", "hailort"),
    ("ocr_det", "vision", "hailort"),
    ("ocr", "vision", "hailort"),
]
_VISION_TASK = {
    "yolov8m": "detection", "yolov8m_pose": "pose", "yolov5m_seg": "segmentation",
    "ufld_v2_tu": "lanes", "scrfd_10g": "dms", "ocr_det": "ocr", "ocr": "ocr",
}


class FakeVision:
    def __init__(self) -> None:
        self.calls = 0

    def tasks(self) -> list[str]:
        return list(TASKS)

    async def detect(self, image, task, conf=0.35, model=None) -> dict:
        self.calls += 1
        h, w = image.shape[:2]
        out: dict = {
            "model": model or TASK_MODEL.get(task, "yolov8m"),
            "task": task,
            "inference_ms": 1.0,
            "image_width": w,
            "image_height": h,
            "detections": [{"class": "person", "confidence": 0.9, "bbox": [10, 20, 110, 220]}],
        }
        if task == "pose":
            out["detections"][0]["keypoints"] = [[60.0, 40.0, 0.9]]
        if task == "lanes":
            out["lanes"] = [[[0, h - 1], [w // 2, h // 2]]]
        if task == "dms":
            out["dms"] = {"status": "attentive", "summary": "Driver attentive: looking ahead",
                          "faces": 1, "head_yaw_deg": 0.0, "eyes_visible": True,
                          "phone_in_hand": False, "eyes_on_road": True}
        if task == "face_track":
            out["detections"] = [{"class": "face", "confidence": 0.95,
                                  "bbox": [w * 0.3, h * 0.2, w * 0.7, h * 0.6],
                                  "landmarks": [[w * 0.4, h * 0.34], [w * 0.6, h * 0.34],
                                                [w * 0.5, h * 0.42], [w * 0.43, h * 0.5],
                                                [w * 0.57, h * 0.5]]}]
        if task == "ocr":
            out["text"] = [{"text": "STOP", "bbox": [10, 20, 110, 60], "confidence": 0.9}]
        return out


class FakeVlm:
    def __init__(self) -> None:
        self.calls = 0

    async def ask(self, image, prompt, model="qwen3-vl", max_tokens=160) -> str:
        self.calls += 1
        return "A fake scene with one person."


class FakeChat:
    TOKENS: ClassVar[list[str]] = ["Hello", " from", " the", " box"]

    def __init__(self) -> None:
        self.cancelled = False
        self.calls = 0
        self.last: dict | None = None

    async def stream(self, model, messages, max_tokens, images=None) -> AsyncIterator[str]:
        self.calls += 1
        self.cancelled = False
        self.last = {"model": model, "messages": messages, "max_tokens": max_tokens,
                     "images": images}
        finished = False
        try:
            for t in self.TOKENS:
                await asyncio.sleep(0)
                yield t
            finished = True
        finally:
            if not finished:
                self.cancelled = True


class FakeAsr:
    async def transcribe(self, pcm16k, language=None) -> str:
        return "hello box"


class FakeTts:
    async def synthesize(self, text, voice=None) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * int(16000 * 0.2))
        return buf.getvalue()


class FakeCameras:
    def __init__(self, names: list[str]) -> None:
        self._names = names
        self.open_count = 0

    def sources(self) -> list[str]:
        return list(self._names)

    async def frames(self, source: str, fps: float) -> AsyncIterator[np.ndarray]:
        if source not in self._names:
            raise KeyError(source)
        grad = np.tile(np.linspace(0, 255, 640, dtype=np.uint8), (360, 1))
        frame = np.stack([grad, grad[::-1], np.full_like(grad, 128)], axis=-1)
        self.open_count += 1
        period = 1.0 / max(fps, 0.1)
        while True:
            await asyncio.sleep(period)
            yield frame.copy()


class FakeGovernor:
    def __init__(self) -> None:
        self.switches: list[str] = []
        self._owner: str | None = None

    def models(self) -> list[dict]:
        return [
            {
                "id": mid, "object": "model", "owned_by": "hailo", "modality": mod,
                "task": _VISION_TASK.get(mid), "runtime": rt, "resident": True,
                "device": "npu" if mod != "tts" else "cpu", "state": "resident",
                "error": None, "load_s": 0.0,
            }
            for mid, mod, rt in FAKE_MODELS
        ]

    async def switch_genai(self, model: str) -> None:
        self.switches.append(model)
        self._owner = model

    def genai_owner(self) -> str | None:
        return self._owner

    def genai_state(self) -> str:
        return "idle"

    def npu_stats(self) -> dict:
        return {"nnc_util_pct": 12.0, "ram_used_mb": 1024.0, "ram_total_mb": 7372.0,
                "temp_c": 55.0}

    def model_stats(self) -> list[dict]:
        return [{"id": m[0], "state": "resident", "fps": 0.0, "p50_ms": 0.0, "p95_ms": 0.0,
                 "requests": 0} for m in FAKE_MODELS]

    def vision_fps_cap(self) -> float | None:
        return None


class FakeFaceAuth:
    """Stand-in for the F455. `present` is the identity of the face in front of the camera."""

    def __init__(self) -> None:
        self._faces: dict[str, str] = {}   # sensor user id -> face identity
        self.present: str | None = "face-A"

    async def users(self) -> list[str]:
        return sorted(self._faces)

    async def enroll(self, user_id: str) -> dict:
        from .backends.realsense_id import validate_user_id

        user_id = validate_user_id(user_id)
        if self.present is None:
            return {"enrolled": False, "user_id": None, "status": "NoFaceDetected",
                    "method": "realsense_id", "device": "f455", "latency_ms": 1.0}
        if self.present in self._faces.values():
            return {"enrolled": False, "user_id": None, "status": "DuplicateFaceprints",
                    "method": "realsense_id", "device": "f455", "latency_ms": 1.0}
        self._faces[user_id] = self.present
        return {"enrolled": True, "user_id": user_id, "status": "Success",
                "method": "realsense_id", "device": "f455", "latency_ms": 1.0}

    async def authenticate(self) -> dict:
        user = next((u for u, f in sorted(self._faces.items()) if f == self.present), None)
        status = "Success" if user else ("NoFaceDetected" if self.present is None else "Forbidden")
        return {"authenticated": user is not None, "user_id": user, "status": status,
                "method": "realsense_id", "device": "f455", "security_level": "Low",
                "algo_flow": "All", "face_selection_policy": "Single", "latency_ms": 1.0}

    async def remove_user(self, user_id: str) -> dict:
        self._faces.pop(user_id, None)
        return {"deleted": True, "user_id": user_id, "method": "realsense_id"}


def make_fake_services(settings: Settings) -> Services:
    return Services(
        vision=FakeVision(),
        vlm=FakeVlm(),
        chat=FakeChat(),
        asr=FakeAsr(),
        tts=FakeTts(),
        cameras=FakeCameras(list(settings.cameras)),
        governor=FakeGovernor(),
        inspector=Inspector(),
        settings=settings,
        face_auth=FakeFaceAuth(),
    )
