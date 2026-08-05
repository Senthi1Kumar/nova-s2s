"""Matrix-only TTS adapters for experimental ORT backends (Supertonic 3, Pocket TTS).

Caller: scripts/run_model_matrix.py run_tts_candidate. Production factory
(nova_hailo/backends/tts.py) is deliberately NOT touched — per plan, the matrix
selects a winner before anything is promoted.

Each factory returns (synth, release, api_label) where synth(text) ->
(float32 mono ndarray, sample_rate). Raises RuntimeError with a precise
provisioning reason when the runtime/artifact is unavailable (the runner
records it as an unmeasured row, never a crash).

Licenses (recorded in the manifest, restated here because they gate the demo):
- Supertonic 3: code MIT, model OpenRAIL-M (Supertone/supertonic-3).
- Pocket TTS sherpa-onnx int8 export: **NON-COMMERCIAL** (community export of
  kyutai/pocket-tts). Benchmark/demo only — not OEM-shippable.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np

SynthFn = Callable[[str], tuple[np.ndarray, int]]
ReleaseFn = Callable[[], None]

SUPERTONIC_VOICE = "M4"
POCKET_NUM_STEPS = 5


def _mono_f32(audio) -> np.ndarray:
    x = np.asarray(audio, dtype=np.float32)
    if x.ndim > 1:
        x = x.reshape(-1) if 1 in x.shape else x.mean(axis=0)
    return np.ascontiguousarray(x.reshape(-1))


def create_supertonic() -> tuple[SynthFn, ReleaseFn, str]:
    """Official Supertonic 3 via the `supertonic` PyPI package (onnxruntime CPU).

    First call downloads ~260 MB from HF into the local cache; provision before
    benchmarking so cold_load measures ONNX session init, not the network.
    """
    try:
        from supertonic import TTS
    except ImportError as exc:
        raise RuntimeError(f"supertonic package not installed: {exc}") from exc

    tts = TTS(auto_download=True)
    style = tts.get_voice_style(voice_name=SUPERTONIC_VOICE)
    sr = int(getattr(tts, "sample_rate", 44100))

    def synth(text: str) -> tuple[np.ndarray, int]:
        wav, _duration = tts.synthesize(text, voice_style=style)
        return _mono_f32(wav), sr

    def release() -> None:
        pass  # plain ORT sessions; GC is sufficient

    return synth, release, "supertonic3_onnx_real"


def create_pocket_sherpa(model_dir: Path) -> tuple[SynthFn, ReleaseFn, str]:
    """Pocket TTS int8 via sherpa-onnx OfflineTts (voice cloned from bundled ref wav)."""
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise RuntimeError(f"sherpa_onnx package not installed: {exc}") from exc
    import soundfile as sf

    d = Path(model_dir)
    needed = {
        "lm_flow": d / "lm_flow.int8.onnx",
        "lm_main": d / "lm_main.int8.onnx",
        "encoder": d / "encoder.onnx",
        "decoder": d / "decoder.int8.onnx",
        "text_conditioner": d / "text_conditioner.onnx",
        "vocab_json": d / "vocab.json",
        "token_scores_json": d / "token_scores.json",
    }
    missing = [p.name for p in needed.values() if not p.is_file()]
    if missing:
        raise RuntimeError(f"pocket sherpa-onnx artifacts missing in {d}: {missing}")

    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            pocket=sherpa_onnx.OfflineTtsPocketModelConfig(
                **{k: str(v) for k, v in needed.items()}
            ),
            debug=False,
            num_threads=2,
            provider="cpu",
        )
    )
    if not cfg.validate():
        raise RuntimeError("sherpa-onnx pocket config failed validation")
    tts = sherpa_onnx.OfflineTts(cfg)

    ref_wav = d / "test_wavs" / "bria.wav"
    if not ref_wav.is_file():
        raise RuntimeError(f"pocket reference voice missing: {ref_wav}")
    ref_audio, ref_sr = sf.read(str(ref_wav), dtype="float32")
    ref_audio = _mono_f32(ref_audio)

    gen_config = sherpa_onnx.GenerationConfig()
    gen_config.reference_audio = ref_audio
    gen_config.reference_sample_rate = int(ref_sr)
    gen_config.num_steps = POCKET_NUM_STEPS

    def synth(text: str) -> tuple[np.ndarray, int]:
        audio = tts.generate(text, gen_config)
        return _mono_f32(audio.samples), int(audio.sample_rate)

    def release() -> None:
        pass

    return synth, release, "pocket_sherpa_onnx_int8_real"


def create_matrix_tts(backend: str, artifact: Path | None) -> tuple[SynthFn, ReleaseFn, str]:
    if backend == "supertonic_onnx":
        return create_supertonic()
    if backend == "pocket_sherpa_onnx":
        if artifact is None:
            raise RuntimeError("pocket sherpa-onnx artifact not declared/found")
        # Manifest declares one .onnx file inside the model dir (find_declared_artifact
        # requires a file); the adapter needs the whole directory.
        return create_pocket_sherpa(Path(artifact).parent)
    raise RuntimeError(f"no matrix adapter for backend {backend!r}")
