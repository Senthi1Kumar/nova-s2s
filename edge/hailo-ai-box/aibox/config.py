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
    audio_profile: str = "auto"
    audio_capture_device: str = ""
    audio_playback_device: str = ""
    turn_judge_backend: str = "timeout"
    d3_model_id: str = "vllm-sr/d3-edge"
    asr_language: str = "en"
    asr_model: str = "whisper-small"
    tts_backend: str = "phonon"
    phonon_duration_delta: float = -0.25
    voice_vad: bool = True
    voice_vad_threshold: float = 0.5
    voice_min_speech_ms: int = 300
    voice_min_speech_ratio: float = 0.15
    voice_partials: bool = True
    voice_barge_in: bool = False


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def _flag(v: str | None, default: bool) -> bool:
    return default if v is None or not v.strip() else _truthy(v)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    d = Settings()
    asr_model = (e.get("AIBOX_ASR_MODEL") or d.asr_model).strip().lower()
    if asr_model not in {"whisper-tiny", "whisper-base", "whisper-small"}:
        raise ValueError(f"AIBOX_ASR_MODEL must be whisper-tiny, whisper-base or whisper-small, got {asr_model!r}")
    tts_backend = (e.get("AIBOX_TTS_BACKEND") or d.tts_backend).strip().lower()
    if tts_backend not in {"phonon", "piper"}:
        raise ValueError(f"AIBOX_TTS_BACKEND must be phonon or piper, got {tts_backend!r}")
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
        audio_profile=e.get("AIBOX_AUDIO_PROFILE") or d.audio_profile,
        audio_capture_device=e.get("NOVA_CAPTURE_DEVICE") or d.audio_capture_device,
        audio_playback_device=e.get("NOVA_PLAYBACK_DEVICE") or d.audio_playback_device,
        turn_judge_backend=e.get("AIBOX_TURN_JUDGE") or d.turn_judge_backend,
        d3_model_id=e.get("AIBOX_D3_MODEL_ID") or d.d3_model_id,
        asr_language=e.get("AIBOX_ASR_LANGUAGE") or d.asr_language,
        asr_model=asr_model,
        tts_backend=tts_backend,
        phonon_duration_delta=float(e.get("AIBOX_PHONON_DURATION_DELTA") or d.phonon_duration_delta),
        voice_vad=_flag(e.get("AIBOX_VOICE_VAD"), d.voice_vad),
        voice_vad_threshold=float(e.get("AIBOX_VOICE_VAD_THRESHOLD") or d.voice_vad_threshold),
        voice_min_speech_ms=int(e.get("AIBOX_VOICE_MIN_SPEECH_MS") or d.voice_min_speech_ms),
        voice_min_speech_ratio=float(
            e.get("AIBOX_VOICE_MIN_SPEECH_RATIO") or d.voice_min_speech_ratio),
        voice_partials=_flag(e.get("AIBOX_VOICE_PARTIALS"), d.voice_partials),
        voice_barge_in=_flag(e.get("AIBOX_VOICE_BARGE_IN"), d.voice_barge_in),
    )
