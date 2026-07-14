#!/usr/bin/env python3
"""One-off utility: synthesize a 16kHz mono PCM16 WAV fixture for demo_e2e.py
using the existing edge-track Kokoro TTS (nova/engine/tts.py).

This is NOT part of the demo path (run_demo.py / tool_service.py never import
nova.engine) -- it's a manual, offline fixture-generation step, run once to
produce tests/fixtures/asr/set_ac_20.wav, per Task 7's brief ("reusing an
existing TTS capability to produce a test WAV file" is fine; wiring
nova/engine into the demo path itself is not).

Usage:
    uv run python scripts/_gen_fixture_wav.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from nova.engine.tts import KokoroTTS

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = REPO_ROOT / "runtime" / "models" / "kokoro-82m"
OUT_PATH = REPO_ROOT / "tests" / "fixtures" / "asr" / "set_ac_20.wav"
TEXT = "Please turn on the air conditioning and set it to twenty degrees."


def main() -> None:
    tts = KokoroTTS.load(MODEL_DIR, prewarm=False)
    result = tts.synthesize(TEXT)
    audio_24k = result.audio.astype(np.float32)

    # Resample 24kHz -> 16kHz mono PCM16 to match the ASR fixture format
    # (tests/fixtures/asr/beckett_5s.wav is 16kHz/mono/PCM16; s2s_smoke.py's
    # load_pcm16 asserts exactly that format).
    audio_16k = resample_poly(audio_24k, up=2, down=3)
    audio_16k = np.clip(audio_16k, -1.0, 1.0)
    pcm16 = (audio_16k * 32767.0).astype(np.int16)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    sf.write(OUT_PATH, pcm16, 16000, subtype="PCM_16")
    print(f"Wrote {OUT_PATH} ({len(pcm16) / 16000:.2f}s, text={TEXT!r})")


if __name__ == "__main__":
    main()
