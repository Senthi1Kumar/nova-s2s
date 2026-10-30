"""Settings: env vars with defaults. Never reads a .env file itself."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

# By camera model, not unit: /dev/v4l/by-id names carry each camera's serial number. Patterns are
# resolved when a stream opens, so a camera plugged in later works too. The USB source ("c920e")
# prefers a Logitech C920e and falls back to any other USB webcam.
DEFAULT_CAMERAS = {
    "c920e": ("/dev/v4l/by-id/usb-046d_Logi_Webcam_C920e_*-video-index0"
              "|/dev/v4l/by-id/usb-*-video-index0"),
    "f455": "/dev/v4l/by-id/usb-Intel_Intel_F45*-video-index0",
}


REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass(frozen=True)
class Settings:
    https_port: int = 8443
    http_port: int = 8442
    ollama_url: str = "http://127.0.0.1:8000"
    hef_dir: str = "/usr/local/hailo/resources/models/hailo10h"
    qwen3_vl_hef: str = os.path.expanduser("~/Downloads/Qwen3-VL-2B-Instruct.hef")
    piper_voice: str = os.path.join(REPO_DIR, "models", "piper", "en_US-amy-low.onnx")
    cameras: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CAMERAS))
    realsense_lib: str = os.path.expanduser("~/repos/RealSenseID/build-device/lib")
    api_key: str | None = None
    fake: bool = False
    video_dir: str = os.path.join(REPO_DIR, "data", "videos")
    upload_dir: str = os.path.join(REPO_DIR, "runtime", "videos")
    face_roster: str = os.path.join(REPO_DIR, "runtime", "face_roster.json")


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    d = Settings()
    return Settings(
        https_port=int(e.get("AIBOX_HTTPS_PORT") or d.https_port),
        http_port=int(e.get("AIBOX_HTTP_PORT") or d.http_port),
        ollama_url=e.get("AIBOX_OLLAMA_URL") or d.ollama_url,
        hef_dir=e.get("AIBOX_HEF_DIR") or d.hef_dir,
        qwen3_vl_hef=e.get("AIBOX_QWEN3_VL_HEF") or d.qwen3_vl_hef,
        piper_voice=e.get("AIBOX_PIPER_VOICE") or d.piper_voice,
        cameras={
            "c920e": e.get("AIBOX_CAMERA_C920E") or DEFAULT_CAMERAS["c920e"],
            "f455": e.get("AIBOX_CAMERA_F455") or DEFAULT_CAMERAS["f455"],
        },
        realsense_lib=e.get("AIBOX_REALSENSE_LIB") or d.realsense_lib,
        api_key=e.get("AIBOX_API_KEY") or None,
        fake=_truthy(e.get("AIBOX_FAKE")),
        video_dir=e.get("AIBOX_VIDEO_DIR") or d.video_dir,
        upload_dir=e.get("AIBOX_UPLOAD_DIR") or d.upload_dir,
        face_roster=e.get("AIBOX_FACE_ROSTER") or d.face_roster,
    )
