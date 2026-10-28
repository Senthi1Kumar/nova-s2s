"""Service Protocols the routers depend on, plus the Services bundle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import numpy as np

from .config import Settings
from .inspector import Inspector


class SlotBusy(Exception):
    """GenAI slot is swapping and the queue is full; routers map this to 503 + Retry-After."""

    def __init__(self, retry_after_s: float = 5.0, message: str = "GenAI slot is busy") -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


@runtime_checkable
class VisionService(Protocol):
    def tasks(self) -> list[str]: ...

    async def detect(
        self, image: np.ndarray, task: str, conf: float = 0.35, model: str | None = None
    ) -> dict: ...


@runtime_checkable
class VlmService(Protocol):
    async def ask(
        self, image: np.ndarray, prompt: str, model: str = "qwen3-vl", max_tokens: int = 160
    ) -> str: ...


@runtime_checkable
class ChatService(Protocol):
    def stream(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int,
        images: list[np.ndarray] | None = None,
    ) -> AsyncIterator[str]: ...


@runtime_checkable
class AsrService(Protocol):
    async def transcribe(self, pcm16k: np.ndarray, language: str | None = None) -> str: ...


@runtime_checkable
class TtsService(Protocol):
    async def synthesize(self, text: str, voice: str | None = None) -> bytes: ...


@runtime_checkable
class CameraService(Protocol):
    def sources(self) -> list[str]: ...

    def frames(self, source: str, fps: float) -> AsyncIterator[np.ndarray]: ...


@runtime_checkable
class Governor(Protocol):
    def models(self) -> list[dict]: ...

    async def switch_genai(self, model: str) -> None: ...

    def genai_owner(self) -> str | None: ...

    def genai_state(self) -> str: ...

    def npu_stats(self) -> dict: ...

    def model_stats(self) -> list[dict]: ...

    def vision_fps_cap(self) -> float | None: ...


@runtime_checkable
class FaceAuthService(Protocol):
    async def users(self) -> list[str]: ...

    async def enroll(self, user_id: str) -> dict: ...

    async def authenticate(self) -> dict: ...

    async def remove_user(self, user_id: str) -> dict: ...


@dataclass
class Services:
    vision: VisionService
    vlm: VlmService
    chat: ChatService
    asr: AsrService
    tts: TtsService
    cameras: CameraService
    governor: Governor
    inspector: Inspector
    settings: Settings
    face_auth: FaceAuthService | None = None
    face_roster: Any = None   # FaceRoster, created lazily from settings.face_roster
