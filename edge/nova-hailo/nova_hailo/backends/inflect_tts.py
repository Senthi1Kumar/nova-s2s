"""Inflect-Nano-v2 TTS via its torch-free ONNX runtime.

Chosen over the autoregressive compact TTS models (MOSS-TTS-Nano, Audio8) for
one structural reason: Inflect is a VITS-style one-shot decoder, so synthesis
cost tracks utterance length linearly and predictably instead of running a
token-by-token loop. MOSS measured >6 min for a single sentence on this Pi;
Piper, also VITS-family, does the same sentence in ~250 ms.

Two graphs totalling ~15.5 MB (duration.onnx 3.5 MB + decode.onnx 12 MB) and a
published steady-state RTF of 0.0933 on 4 threads. Output is 24 kHz mono
float32 already clipped to [-1, 1], returned in memory -- no temp-file round
trip, unlike MOSS.

Upstream's `InflectONNX.synthesize()` returns `(sample_rate, waveform)`; the
QueuedOnnxTTS contract is `(waveform, sample_rate)`, so the tuple is swapped
here.

Frontend needs espeak-ng via `espeakng-loader` plus `phonemizer`; Piper already
pulls espeak-ng in, so the system dependency is shared.
"""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

import numpy as np

from nova_hailo.backends.tts import QueuedOnnxTTS

logger = logging.getLogger(__name__)

INFLECT_SAMPLE_RATE = 24000
DEFAULT_REPO_DIRNAME = "Inflect-Nano-v2-ONNX"


def _add_repo_to_path(repo: Path) -> Path:
    onnx_dir = str(repo / "onnx")
    if onnx_dir not in sys.path:
        sys.path.insert(0, onnx_dir)
    return repo


def _looks_like_repo(p: Path) -> bool:
    return (p / "onnx" / "inference_onnx.py").is_file()


def _discover_repo() -> Path | None:
    """Inflect ships as a repo, not a package, so locate it and path-inject.

    Kept local to this backend rather than pushed into the launcher.
    """
    root = Path(__file__).resolve().parents[2]
    for cand in (
        os.environ.get("NOVA_INFLECT_REPO"),
        root / "cloned" / DEFAULT_REPO_DIRNAME,
        root / "models" / DEFAULT_REPO_DIRNAME,
    ):
        if not cand:
            continue
        p = Path(cand).expanduser()
        if _looks_like_repo(p):
            return _add_repo_to_path(p)
    return None


class InflectTTS(QueuedOnnxTTS):
    """Inflect-Nano-v2 ONNX, same contract as PiperTTS/KokoroTTS."""

    device = "cpu_onnx_inflect"

    def __init__(
        self,
        enabled: bool = True,
        model_dir: str | None = None,
        speed: float = 1.0,
        variation: float = 0.667,
        seed: int = 7,
        device_id: int | None = None,
        wait_for_play: bool = False,
        local_play: bool = True,
        pcm_callback: Callable[[np.ndarray, int], None] | None = None,
    ):
        super().__init__(
            enabled=enabled,
            device_id=device_id,
            wait_for_play=wait_for_play,
            local_play=local_play,
            pcm_callback=pcm_callback,
        )
        self._engine = None
        self._speed = float(speed)
        self._variation = float(variation)
        # Fixed seed by default: an in-car assistant should not re-roll prosody
        # between identical prompts, and it keeps A/B runs comparable.
        self._seed = int(seed)
        if not enabled:
            return
        try:
            repo = self._resolve_repo(model_dir)
            if repo is None:
                raise FileNotFoundError(
                    f"Inflect repo not found (expected cloned/{DEFAULT_REPO_DIRNAME}, "
                    "models/, or $NOVA_INFLECT_REPO)"
                )
            from inference_onnx import InflectONNX

            self.model_path = str(repo)
            logger.info("Loading Inflect-Nano-v2: %s", repo)
            print(f"Loading Inflect TTS: {repo}")
            self._engine = InflectONNX(model_dir=str(repo), provider="cpu")
            self._start_worker()
        except Exception as e:
            logger.warning("Inflect TTS disabled: %s", e)
            print(f"Inflect TTS disabled: {e}")
            self.enabled = False

    @staticmethod
    def _resolve_repo(model_dir: str | None) -> Path | None:
        explicit = model_dir or os.environ.get("NOVA_INFLECT_MODEL_DIR")
        if not explicit:
            return _discover_repo()
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = Path(__file__).resolve().parents[2] / p
        return _add_repo_to_path(p) if _looks_like_repo(p) else None

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        if self._engine is None:
            return np.zeros(0, dtype=np.float32), INFLECT_SAMPLE_RATE
        try:
            sample_rate, waveform = self._engine.synthesize(
                text,
                speed=self._speed,
                variation=self._variation,
                seed=self._seed,
            )
        except ValueError as e:
            # Upstream raises on empty text; a trimmed clause can reach here.
            logger.debug("Inflect skipped %r: %s", text, e)
            return np.zeros(0, dtype=np.float32), INFLECT_SAMPLE_RATE
        if self._interrupt.is_set():
            return np.zeros(0, dtype=np.float32), int(sample_rate)
        arr = np.asarray(waveform, dtype=np.float32).reshape(-1)
        return arr, int(sample_rate)
