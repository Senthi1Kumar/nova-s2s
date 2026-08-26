"""FireRedVAD streaming (ONNX + kaldi_native_fbank) — Xiaohongshu FireRedASR2S.

Callers: nova_hailo/web/vad.py create_vad_segmenter(), realtime_session.
API: FireRedVadSegmenter.feed(pcm16) -> [(speech_started|speech_stopped, audio|None)]
Model files: models/firered_vad/{fireredvad_stream_vad_with_cache.onnx, cmvn.npz}.

0.6M-param DFSMN, 10ms causal frames, cache-based streaming (arXiv:2603.10420).
Measured (FLEURS-VAD-102): F1 97.57 / FAR 2.69% vs Silero's F1 95.95 / FAR 9.41%.
This is a generic VAD upgrade, not by itself a fix for self-barge: it has no
target-speaker conditioning, unlike FireRedChat's separate, unreleased pVAD,
so it won't stop the device from hearing its own TTS output as speech.

Two scale conventions differ from Silero and are easy to get backwards:
  - FireRedVAD's fbank frontend expects int16-amplitude samples (kaldi
    convention); Silero's model expects [-1, 1] float samples. Feeding
    normalized floats here silently collapses every probability toward zero
    (verified: max prob 0.03 across an entire utterance) with no error raised.
  - The turn-taking state machine (pre-roll, min-speech, min-silence,
    max-utterance) is a direct port of SileroVadSegmenter's -- it is
    VAD-model agnostic. Only the per-frame probability primitive and its
    frame period (10ms here vs Silero's 32ms WINDOW) differ, so the two
    engines are an apples-to-apples A/B on frontend accuracy, not on
    differing endpointing tunables.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from nova_hailo.config import ROOT

try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover
    ort = None

try:
    import kaldi_native_fbank as knf
except ImportError:  # pragma: no cover
    knf = None

SAMPLE_RATE = 16000
FRAME_SHIFT_MS = 10
FRAME_SHIFT_SAMPLE = int(SAMPLE_RATE * FRAME_SHIFT_MS / 1000)  # 160
NUM_MEL_BINS = 80
NUM_CACHES = 8
CACHE_SHAPE = (1, 128, 19)  # (P, lookback_padding) per FSMN block, from export_onnx.py

MODEL_DIR = ROOT / "models" / "firered_vad"
DEFAULT_ONNX = MODEL_DIR / "fireredvad_stream_vad_with_cache.onnx"
DEFAULT_CMVN = MODEL_DIR / "cmvn.npz"
HF_REPO = "FireRedTeam/FireRedVAD"
HF_SUBDIR = "Stream-VAD"


def ensure_firered_assets(
    onnx_path: Path | None = None, cmvn_path: Path | None = None
) -> tuple[Path, Path]:
    """Return (onnx, cmvn_npz) paths; fetch from HF once if missing.

    HF ships a .pth.tar + cmvn.ark, not the ONNX graph, so a first run exports
    the streaming-with-cache graph and pre-computes CMVN stats into a plain
    npz -- after that, no torch/kaldiio import is needed at inference time.
    """
    onnx = Path(onnx_path) if onnx_path else DEFAULT_ONNX
    cmvn = Path(cmvn_path) if cmvn_path else DEFAULT_CMVN
    if onnx.exists() and cmvn.exists():
        return onnx, cmvn

    onnx.parent.mkdir(parents=True, exist_ok=True)
    import math
    import tempfile

    from huggingface_hub import hf_hub_download

    print(f"Fetching FireRedVAD ({HF_REPO}/{HF_SUBDIR}) ...")
    with tempfile.TemporaryDirectory() as tmp:
        ckpt = hf_hub_download(HF_REPO, f"{HF_SUBDIR}/model.pth.tar", local_dir=tmp)
        ark = hf_hub_download(HF_REPO, f"{HF_SUBDIR}/cmvn.ark", local_dir=tmp)

        if not cmvn.exists():
            import kaldiio

            stats = kaldiio.load_mat(ark)
            dim = stats.shape[-1] - 1
            count = stats[0, dim]
            means, istds = [], []
            for d in range(dim):
                mean = stats[0, d] / count
                means.append(float(mean))
                var = (stats[1, d] / count) - mean * mean
                if var < 1e-20:
                    var = 1e-20
                istds.append(1.0 / math.sqrt(var))
            np.savez(
                cmvn,
                mean=np.array(means, dtype=np.float32),
                istd=np.array(istds, dtype=np.float32),
            )

        if not onnx.exists():
            _export_onnx_with_cache(ckpt, onnx)

    return onnx, cmvn


def _export_onnx_with_cache(ckpt_path: str, out_path: Path) -> None:
    """One-time torch export of the streaming-with-cache graph. Not on the
    inference hot path -- only runs the first time assets are missing."""
    import torch
    from fireredvad.core.detect_model import DetectModel

    model = DetectModel.from_pretrained(str(Path(ckpt_path).parent))
    model.eval()

    class _Wrapper(torch.nn.Module):
        def __init__(self, m, n):
            super().__init__()
            self.model, self.n = m, n

        def forward(self, feat, caches_in):
            probs, new_caches = self.model(feat, caches=[caches_in[i] for i in range(self.n)])
            return probs, torch.stack(new_caches)

    wrapper = _Wrapper(model, NUM_CACHES)
    dummy_feat = torch.randn(1, 1, NUM_MEL_BINS)
    dummy_caches = torch.zeros(NUM_CACHES, *CACHE_SHAPE)
    torch.onnx.export(
        wrapper,
        (dummy_feat, dummy_caches),
        str(out_path),
        input_names=["feat", "caches_in"],
        output_names=["probs", "caches_out"],
        dynamic_axes={"feat": {1: "time"}, "probs": {1: "time"}},
        opset_version=18,
        dynamo=False,
    )


@dataclass
class FireRedVadConfig:
    sample_rate: int = SAMPLE_RATE
    speech_threshold: float = 0.5
    # Same tuning rationale as Silero's config (see silero_vad.py): a short
    # min_silence endpoints mid-sentence and yields fragments ASR mangles.
    min_silence_ms: int = 600
    min_speech_ms: int = 400
    speech_pad_ms: int = 400
    max_utterance_s: float = 8.0
    onnx_path: str | None = None
    cmvn_path: str | None = None

    def __post_init__(self):
        if not 0.0 <= self.speech_threshold <= 1.0:
            raise ValueError("speech_threshold must be in [0, 1]")


class _FireRedOnnx:
    """Cache-based streaming inference: feed 80-dim fbank frames one at a time."""

    def __init__(self, onnx_path: str, cmvn_path: str):
        if ort is None:
            raise RuntimeError("onnxruntime not installed")
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.session = ort.InferenceSession(
            onnx_path, providers=["CPUExecutionProvider"], sess_options=opts
        )
        cmvn = np.load(cmvn_path)
        self._mean = cmvn["mean"].astype(np.float32)
        self._istd = cmvn["istd"].astype(np.float32)
        self.reset_states()

    def reset_states(self):
        self._caches = np.zeros((NUM_CACHES, *CACHE_SHAPE), dtype=np.float32)

    def __call__(self, feat_frame: np.ndarray) -> float:
        """feat_frame: raw (unnormalized) fbank frame, shape (80,)."""
        x = (np.asarray(feat_frame, dtype=np.float32) - self._mean) * self._istd
        x = x.reshape(1, 1, NUM_MEL_BINS)
        probs, self._caches = self.session.run(
            None, {"feat": x, "caches_in": self._caches}
        )
        return float(np.asarray(probs).reshape(-1)[0])


def _make_fbank():
    if knf is None:
        raise RuntimeError("kaldi_native_fbank not installed")
    opts = knf.FbankOptions()
    opts.frame_opts.samp_freq = SAMPLE_RATE
    opts.frame_opts.frame_length_ms = 25
    opts.frame_opts.frame_shift_ms = FRAME_SHIFT_MS
    opts.frame_opts.dither = 0.0
    opts.frame_opts.snip_edges = True
    opts.mel_opts.num_bins = NUM_MEL_BINS
    return knf.OnlineFbank(opts)


class FireRedVadSegmenter:
    """Same event API as SileroVadSegmenter: speech_started / speech_stopped."""

    def __init__(self, cfg: FireRedVadConfig | None = None):
        self.cfg = cfg or FireRedVadConfig()
        onnx, cmvn = ensure_firered_assets(
            Path(self.cfg.onnx_path) if self.cfg.onnx_path else None,
            Path(self.cfg.cmvn_path) if self.cfg.cmvn_path else None,
        )
        self._model = _FireRedOnnx(str(onnx), str(cmvn))
        self._fbank = _make_fbank()
        self._frames_popped = 0
        # FIFO of not-yet-consumed float32 [-1,1] samples, drained exactly
        # FRAME_SHIFT_SAMPLE at a time whenever a new fbank frame is ready.
        # Kept separate from what feeds the fbank frontend (int16 amplitude).
        self._pending: deque[np.ndarray] = deque()
        self._pending_len = 0

        self._utterance: list[np.ndarray] = []
        self._pre: list[np.ndarray] = []
        self._pre_samples = 0
        self._triggered = False
        self._temp_end = 0
        self._sample_i = 0
        self._active_speech = 0
        self._pad = int(self.cfg.sample_rate * self.cfg.speech_pad_ms / 1000)
        self._min_sil = int(self.cfg.sample_rate * self.cfg.min_silence_ms / 1000)
        self._min_speech = int(self.cfg.sample_rate * self.cfg.min_speech_ms / 1000)
        self._max_utt = int(self.cfg.max_utterance_s * self.cfg.sample_rate)
        self._neg = max(0.01, self.cfg.speech_threshold - 0.15)
        print(f"VAD: FireRedVAD ONNX ({onnx.name}) thr={self.cfg.speech_threshold}")

    def set_threshold(self, threshold: float):
        t = max(0.0, min(1.0, float(threshold)))
        self.cfg.speech_threshold = 0.38 + t * 0.38
        self._neg = max(0.01, self.cfg.speech_threshold - 0.15)
        self.cfg.min_silence_ms = int(500 + (1.0 - t) * 250)
        self._min_sil = int(self.cfg.sample_rate * self.cfg.min_silence_ms / 1000)

    def reset(self):
        self._model.reset_states()
        self._fbank = _make_fbank()
        self._frames_popped = 0
        self._pending.clear()
        self._pending_len = 0
        self._utterance.clear()
        self._pre.clear()
        self._pre_samples = 0
        self._triggered = False
        self._temp_end = 0
        self._sample_i = 0
        self._active_speech = 0

    def _remember_pre(self, chunk: np.ndarray):
        self._pre.append(chunk.copy())
        self._pre_samples += len(chunk)
        while self._pre and self._pre_samples > self._pad:
            first = self._pre[0]
            if self._pre_samples - len(first) >= self._pad:
                self._pre.pop(0)
                self._pre_samples -= len(first)
            else:
                drop = self._pre_samples - self._pad
                self._pre[0] = first[drop:]
                self._pre_samples -= drop
                break

    def _pop_hop(self) -> np.ndarray:
        """Drain exactly FRAME_SHIFT_SAMPLE float32 samples off the FIFO."""
        out = np.zeros(FRAME_SHIFT_SAMPLE, dtype=np.float32)
        filled = 0
        while filled < FRAME_SHIFT_SAMPLE and self._pending:
            head = self._pending[0]
            take = min(len(head), FRAME_SHIFT_SAMPLE - filled)
            out[filled : filled + take] = head[:take]
            filled += take
            self._pending_len -= take
            if take == len(head):
                self._pending.popleft()
            else:
                self._pending[0] = head[take:]
        return out[:filled] if filled < FRAME_SHIFT_SAMPLE else out

    def _step(self, prob: float, chunk: np.ndarray) -> tuple[str, np.ndarray | None] | None:
        """One 10ms state-machine tick; mirrors SileroVadSegmenter's inner loop."""
        self._sample_i += FRAME_SHIFT_SAMPLE

        if prob >= self.cfg.speech_threshold and self._temp_end:
            self._temp_end = 0

        if prob >= self.cfg.speech_threshold and not self._triggered:
            self._triggered = True
            self._utterance = list(self._pre) + [chunk.copy()]
            self._pre.clear()
            self._pre_samples = 0
            self._active_speech = FRAME_SHIFT_SAMPLE
            return ("speech_started", None)

        if not self._triggered:
            self._remember_pre(chunk)
            return None

        self._utterance.append(chunk.copy())
        if prob >= self._neg:
            self._active_speech += FRAME_SHIFT_SAMPLE

        utt_len = sum(len(c) for c in self._utterance)
        if utt_len >= self._max_utt:
            audio = self._finish()
            return ("speech_stopped", audio) if audio is not None else None

        if prob < self._neg:
            if not self._temp_end:
                self._temp_end = self._sample_i
            if self._sample_i - self._temp_end < self._min_sil:
                return None
            audio = self._finish()
            return ("speech_stopped", audio) if audio is not None else None

        return None

    def feed(self, pcm16: bytes) -> list[tuple[str, np.ndarray | None]]:
        events: list[tuple[str, np.ndarray | None]] = []
        if not pcm16:
            return events

        i16 = np.frombuffer(pcm16, dtype=np.int16)
        self._fbank.accept_waveform(self.cfg.sample_rate, i16.tolist())
        self._pending.append(i16.astype(np.float32) / 32768.0)
        self._pending_len += len(i16)

        while self._frames_popped < self._fbank.num_frames_ready:
            frame = np.asarray(self._fbank.get_frame(self._frames_popped), dtype=np.float32)
            self._frames_popped += 1
            chunk = self._pop_hop()
            if len(chunk) == 0:
                continue
            prob = self._model(frame)
            ev = self._step(prob, chunk)
            if ev is not None:
                events.append(ev)

        return events

    def _finish(self) -> np.ndarray | None:
        audio = (
            np.concatenate(self._utterance).astype(np.float32)
            if self._utterance
            else np.zeros(0, dtype=np.float32)
        )
        active = self._active_speech
        self._triggered = False
        self._temp_end = 0
        self._utterance.clear()
        self._active_speech = 0
        self._pre.clear()
        self._pre_samples = 0
        if active < self._min_speech or len(audio) < self._min_speech:
            return None
        return audio
