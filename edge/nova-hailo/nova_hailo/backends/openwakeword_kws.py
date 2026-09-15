"""openWakeWord ONNX backend for the Nova wake-word facade.

Callers: nova_hailo.wake_kws.WakeWordDetector, scripts/wakeword_eval.py.

The HMI uplinks ~60 ms PCM16 batches; openWakeWord wants 1280-sample (80 ms)
frames at 16 kHz. This class buffers, then runs Model.predict with explicit
mel/embedding ONNX paths so nothing is downloaded into site-packages at runtime.

openwakeword's package __init__ imports sklearn (custom verifier). We load
``openwakeword.model.Model`` without that path when sklearn is missing.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import time
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from nova_hailo.config import ROOT

logger = logging.getLogger("nova_hailo.kws")

FRAME_SAMPLES = 1280
SAMPLE_RATE = 16000
PredictFn = Callable[[np.ndarray], float]


@dataclass(frozen=True)
class WakewordPaths:
    model: Path
    melspec: Path
    embedding: Path


def resolve_wakeword_paths(
    *,
    root: Path | None = None,
    model_path: str | Path | None = None,
) -> WakewordPaths | None:
    """Locate nova_pro_v3.onnx + openWakeWord frontend ONNX files.

    Search order for the custom head: explicit ``model_path``, then
    ``models/wakeword/``, then ``nova-project/nova_wakeword_runtime/models/``.
    Frontend models stay next to the head when present.
    """
    base = Path(root) if root is not None else ROOT
    search_dirs = [
        base / "models" / "wakeword",
        base / "nova-project" / "nova_wakeword_runtime" / "models",
    ]
    model: Path | None = None
    if model_path:
        candidate = Path(model_path)
        if not candidate.is_absolute():
            candidate = base / candidate
        if candidate.is_file():
            model = candidate
    if model is None:
        for d in search_dirs:
            hit = d / "nova_pro_v3.onnx"
            if hit.is_file():
                model = hit
                break
    if model is None:
        return None

    def _frontend(name: str) -> Path | None:
        local = model.parent / name
        if local.is_file():
            return local
        for d in search_dirs:
            hit = d / name
            if hit.is_file():
                return hit
        return None

    melspec = _frontend("melspectrogram.onnx")
    embedding = _frontend("embedding_model.onnx")
    if melspec is None or embedding is None:
        return None
    return WakewordPaths(model=model, melspec=melspec, embedding=embedding)


def _load_oww_model_class():
    """Return openwakeword.model.Model, skipping sklearn-heavy package init."""
    try:
        from openwakeword.model import Model

        return Model
    except ImportError:
        pass

    spec = importlib.util.find_spec("openwakeword")
    if spec is None or spec.origin is None:
        raise ImportError("openwakeword is not installed")
    pkg_dir = Path(spec.origin).parent
    for key in list(sys.modules):
        if key == "openwakeword" or key.startswith("openwakeword."):
            sys.modules.pop(key, None)

    pkg = types.ModuleType("openwakeword")
    pkg.__path__ = [str(pkg_dir)]
    pkg.__file__ = str(pkg_dir / "__init__.py")
    pkg.MODELS = {}
    pkg.FEATURE_MODELS = {}
    pkg.VAD_MODELS = {}
    pkg.model_class_mappings = {}
    pkg.get_pretrained_model_paths = lambda inference_framework="onnx": []
    sys.modules["openwakeword"] = pkg

    for name in ("utils", "model"):
        mod_name = f"openwakeword.{name}"
        path = pkg_dir / f"{name}.py"
        mspec = importlib.util.spec_from_file_location(mod_name, path)
        if mspec is None or mspec.loader is None:
            raise ImportError(f"cannot load {mod_name} from {path}")
        mod = importlib.util.module_from_spec(mspec)
        sys.modules[mod_name] = mod
        mspec.loader.exec_module(mod)
    return sys.modules["openwakeword.model"].Model


class OpenWakeWordKWS:
    """Buffer 16 kHz PCM16 → 80 ms frames → threshold / consecutive-hits / cooldown."""

    def __init__(
        self,
        *,
        model_path: str | Path | None = None,
        melspec_path: str | Path | None = None,
        embedding_path: str | Path | None = None,
        threshold: float = 0.15,
        consecutive_hits: int = 1,
        cooldown_s: float = 1.5,
        predict_fn: PredictFn | None = None,
        root: Path | None = None,
    ):
        self.threshold = float(threshold)
        self.consecutive_hits = max(1, int(consecutive_hits))
        self.cooldown_s = float(cooldown_s)
        self._buf = np.zeros(0, dtype=np.int16)
        self._hit_count = 0
        self._last_trigger = 0.0
        self.last_score = 0.0
        self.model_path = ""
        self._model = None
        self._predict_fn = predict_fn

        if predict_fn is not None:
            self.model_path = str(model_path or "fake")
            return

        paths = None
        if model_path and melspec_path and embedding_path:
            mp, mel, emb = Path(model_path), Path(melspec_path), Path(embedding_path)
            if mp.is_file() and mel.is_file() and emb.is_file():
                paths = WakewordPaths(model=mp, melspec=mel, embedding=emb)
        if paths is None:
            paths = resolve_wakeword_paths(root=root, model_path=model_path)
        if paths is None:
            raise FileNotFoundError(
                "wake-word ONNX not found (need nova_pro_v3.onnx, "
                "melspectrogram.onnx, embedding_model.onnx under models/wakeword/)"
            )
        self.model_path = str(paths.model)
        logger.info("openWakeWord ONNX %s", self.model_path)
        Model = _load_oww_model_class()
        self._model = Model(
            wakeword_models=[str(paths.model)],
            inference_framework="onnx",
            melspec_model_path=str(paths.melspec),
            embedding_model_path=str(paths.embedding),
        )

    def _score_frame(self, frame: np.ndarray) -> float:
        if self._predict_fn is not None:
            return float(self._predict_fn(frame))
        if self._model is None:
            return 0.0
        prediction = self._model.predict(frame)
        if isinstance(prediction, tuple):
            prediction = prediction[0]
        if not prediction:
            return 0.0
        return float(next(iter(prediction.values())))

    def _maybe_trigger(
        self,
        score: float,
        now: float,
        *,
        threshold: float | None = None,
        ignore_cooldown: bool = False,
    ) -> bool:
        self.last_score = score
        thr = self.threshold if threshold is None else float(threshold)
        if score >= thr:
            self._hit_count += 1
        else:
            self._hit_count = 0
        if self._hit_count < self.consecutive_hits:
            return False
        if not ignore_cooldown and (now - self._last_trigger) < self.cooldown_s:
            return False
        self._last_trigger = now
        self._hit_count = 0
        return True

    def feed_pcm16(
        self,
        pcm: bytes | np.ndarray,
        *,
        threshold: float | None = None,
        ignore_cooldown: bool = False,
    ) -> tuple[bool, float]:
        """Append PCM16 and run every complete 1280-sample frame.

        Returns ``(detected, last_score)``. ``detected`` is True if any frame
        in this batch crossed threshold (with consecutive-hits + cooldown).
        """
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            chunk = np.frombuffer(pcm, dtype=np.int16)
        else:
            arr = np.asarray(pcm)
            if arr.dtype != np.int16:
                arr = np.clip(arr, -32768, 32767).astype(np.int16)
            chunk = arr.reshape(-1)
        if chunk.size == 0:
            return False, self.last_score
        self._buf = np.concatenate((self._buf, chunk))
        detected = False
        score = self.last_score
        now = time.monotonic()
        while self._buf.size >= FRAME_SAMPLES:
            frame = self._buf[:FRAME_SAMPLES]
            self._buf = self._buf[FRAME_SAMPLES:]
            score = self._score_frame(frame)
            if self._maybe_trigger(
                score, now, threshold=threshold, ignore_cooldown=ignore_cooldown
            ):
                detected = True
        return detected, score

    def score_clip(self, pcm16: np.ndarray) -> float:
        """Max score over a whole clip (eval / calibration)."""
        audio = np.asarray(pcm16).reshape(-1)
        if audio.dtype != np.int16:
            audio = np.clip(audio, -32768, 32767).astype(np.int16)
        if self._model is not None and hasattr(self._model, "predict_clip"):
            raw = self._model.predict_clip(audio)
            if isinstance(raw, dict):
                peaks = []
                for val in raw.values():
                    arr = np.asarray(val, dtype=np.float32).reshape(-1)
                    if arr.size:
                        peaks.append(float(arr.max()))
                return max(peaks) if peaks else 0.0
            arr = np.asarray(raw, dtype=np.float32).reshape(-1)
            return float(arr.max()) if arr.size else 0.0
        peak = 0.0
        saved = self._buf
        hits = self._hit_count
        last_t = self._last_trigger
        self._buf = np.zeros(0, dtype=np.int16)
        self._hit_count = 0
        self._last_trigger = 0.0
        self.feed_pcm16(audio)
        peak = float(self.last_score)
        if self._buf.size:
            pad = FRAME_SAMPLES - int(self._buf.size)
            self.feed_pcm16(np.zeros(pad, dtype=np.int16))
            peak = max(peak, float(self.last_score))
        self._buf = saved
        self._hit_count = hits
        self._last_trigger = last_t
        return peak
