"""Streaming DTLN noise suppression for the 16 kHz uplink."""



from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import numpy as np

from nova_hailo.config import ROOT

try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover
    ort = None

BLOCK_LEN = 512
BLOCK_SHIFT = 128
NATIVE_RATE = 16000
MODEL_URL = "https://github.com/breizhn/DTLN/raw/master/pretrained_model"
MODEL_FILES = ("model_1.onnx", "model_2.onnx")
SPEECH_RMS = 0.012
MAX_MAKEUP = 3.5
MASK_SMOOTH = 0.65
# qt_mic_app default mix is 0.5; 1.0 + 3.5× HMI gain clips and keeps VAD open.
DEFAULT_STRENGTH = 0.5
MODELS_DIR = ROOT / "models" / "dtln"


def ensure_models(dest: Path | None = None) -> tuple[Path, Path]:
    d = dest or MODELS_DIR
    d.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name in MODEL_FILES:
        p = d / name
        if not (p.is_file() and p.stat().st_size > 50_000):
            url = f"{MODEL_URL}/{name}"
            print(f"[dtln] downloading {url}", flush=True)
            urllib.request.urlretrieve(url, str(p))
        paths.append(p)
    return paths[0], paths[1]


class DtlnNs:
    """Overlap-add DTLN. process_pcm16 expects 16 kHz mono PCM16."""

    def __init__(self, strength: float = DEFAULT_STRENGTH, model_dir: Path | None = None):
        if ort is None:
            raise RuntimeError("onnxruntime not installed")
        p1, p2 = ensure_models(model_dir)
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._s1 = ort.InferenceSession(
            str(p1), providers=["CPUExecutionProvider"], sess_options=opts
        )
        self._s2 = ort.InferenceSession(
            str(p2), providers=["CPUExecutionProvider"], sess_options=opts
        )
        self._n1 = [i.name for i in self._s1.get_inputs()]
        self._n2 = [i.name for i in self._s2.get_inputs()]
        self._in1 = {
            inp.name: np.zeros(
                [d if isinstance(d, int) else 1 for d in inp.shape], dtype=np.float32
            )
            for inp in self._s1.get_inputs()
        }
        self._in2 = {
            inp.name: np.zeros(
                [d if isinstance(d, int) else 1 for d in inp.shape], dtype=np.float32
            )
            for inp in self._s2.get_inputs()
        }
        self.strength = float(max(0.0, min(1.0, strength)))
        self._in_buf = np.zeros(BLOCK_LEN, dtype=np.float32)
        self._out_buf = np.zeros(BLOCK_LEN, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._mask_avg = None
        self._makeup = 1.0
        self._step(np.zeros(BLOCK_SHIFT, dtype=np.float32))
        print(f"[dtln] ready mix={self.strength}", flush=True)

    def reset(self) -> None:
        for d in (self._in1, self._in2):
            for k in d:
                d[k].fill(0.0)
        self._in_buf.fill(0.0)
        self._out_buf.fill(0.0)
        self._pending = np.zeros(0, dtype=np.float32)
        self._mask_avg = None
        self._makeup = 1.0

    def _step(self, frame: np.ndarray) -> np.ndarray:
        self._in_buf[:-BLOCK_SHIFT] = self._in_buf[BLOCK_SHIFT:]
        self._in_buf[-BLOCK_SHIFT:] = frame
        spec = np.fft.rfft(self._in_buf)
        mag = np.abs(spec).astype(np.float32).reshape(1, 1, -1)
        phase = np.angle(spec)
        self._in1[self._n1[0]] = mag
        out1 = self._s1.run(None, self._in1)
        mask = np.asarray(out1[0], dtype=np.float32)
        if self._mask_avg is None:
            self._mask_avg = mask
        else:
            self._mask_avg = MASK_SMOOTH * self._mask_avg + (1.0 - MASK_SMOOTH) * mask
        self._in1[self._n1[1]] = out1[1]
        estimated = mag * self._mask_avg * np.exp(1j * phase)
        block = np.fft.irfft(estimated).astype(np.float32).reshape(1, 1, -1)
        self._in2[self._n2[0]] = block
        out2 = self._s2.run(None, self._in2)
        self._in2[self._n2[1]] = out2[1]
        self._out_buf[:-BLOCK_SHIFT] = self._out_buf[BLOCK_SHIFT:]
        self._out_buf[-BLOCK_SHIFT:] = 0.0
        self._out_buf += np.squeeze(out2[0]).astype(np.float32)
        den = self._out_buf[:BLOCK_SHIFT].copy()

        in_rms = float(np.sqrt(np.mean(frame * frame)) + 1e-12)
        den_rms = float(np.sqrt(np.mean(den * den)) + 1e-12)
        if in_rms >= SPEECH_RMS:
            desired = min(MAX_MAKEUP, in_rms / den_rms)
            self._makeup = 0.85 * self._makeup + 0.15 * desired
        else:
            self._makeup = 0.9 * self._makeup + 0.1 * 1.0
        den = np.clip(den * self._makeup, -1.0, 1.0)
        s = self.strength
        if s >= 0.999:
            return den
        if s <= 0.001:
            return frame.astype(np.float32)
        return (s * den + (1.0 - s) * frame).astype(np.float32)

    def process_pcm16(self, pcm: bytes) -> bytes:
        if not pcm:
            return pcm
        src = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        self._pending = np.concatenate([self._pending, src])
        chunks = []
        while self._pending.size >= BLOCK_SHIFT:
            frame = self._pending[:BLOCK_SHIFT]
            self._pending = self._pending[BLOCK_SHIFT:]
            chunks.append(self._step(frame))
        if not chunks:
            return b""
        y = np.clip(np.concatenate(chunks), -1.0, 1.0)
        return np.round(y * 32767.0).astype(np.int16).tobytes()


def create_dtln(cfg=None):
    """Return DtlnNs or None. Default OFF; NOVA_HAILO_NS=dtln enables.

    Enhancement currently runs always-on and *ahead* of VAD -- the inverse of
    the target chain (AEC -> VAD -> pVAD gate -> gated enhancement -> ASR). It
    therefore spends CPU on noise-only frames and distorts speech onsets, a
    plausible contributor to clipped leading words and inflated WER. Off by
    default until the gated ordering lands; set voice.ns: dtln (or
    NOVA_HAILO_NS=dtln) to turn it back on for an A/B.
    """
    env = (os.environ.get("NOVA_HAILO_NS") or "").strip().lower()
    name = env
    strength = DEFAULT_STRENGTH
    if cfg is not None and not name:
        name = str(cfg.get("voice", "ns", default="off") or "off").lower()
        strength = float(cfg.get("voice", "ns_strength", default=DEFAULT_STRENGTH) or DEFAULT_STRENGTH)
    if not name:
        name = "off"
    if name in {"off", "none", "0", "false"}:
        print("[dtln] disabled", flush=True)
        return None
    try:
        return DtlnNs(strength=strength)
    except Exception as exc:
        print(f"[dtln] unavailable ({exc}) — raw uplink", flush=True)
        return None
