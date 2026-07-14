"""Load the Kokoro-82M TTS model and synthesize one sentence end-to-end.

Usage: uv run python scripts/tts_smoke_test.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from nova.engine.tts import KokoroTTS

N_TIMED_RUNS = 3  # steady-state median; load() already pre-warms

ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = ROOT / "runtime" / "models" / "kokoro-82m"
OUTPUT_PATH = ROOT / "runtime" / "tts_smoke_out.wav"
FIXTURES = ROOT / "tests" / "fixtures" / "tts"


def compute_rms(audio: np.ndarray) -> float:
    """Compute RMS (root mean square) of audio signal."""
    return float(np.sqrt(np.mean(audio ** 2)))


def main() -> int:
    print(f"Loading model from {MODEL_PATH} (threads={os.cpu_count()}, pre-warming) ...")
    t_load = time.perf_counter()
    try:
        tts = KokoroTTS.load(MODEL_PATH, voice="af_heart")
    except FileNotFoundError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"Loaded + pre-warmed in {time.perf_counter() - t_load:.2f}s")

    # Read the golden sentence
    sentence_path = FIXTURES / "golden_sentence.txt"
    try:
        text = sentence_path.read_text().strip()
    except FileNotFoundError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(f"Text: {text!r}")

    # Steady-state timing: median of N runs (load() already paid cold-start)
    runs = [tts.synthesize(text) for _ in range(N_TIMED_RUNS)]
    totals = [sum(r.stage_seconds.values()) for r in runs]
    median_idx = totals.index(sorted(totals)[len(totals) // 2])
    result = runs[median_idx]

    print(f"\nStage timings (median run of {N_TIMED_RUNS}):")
    for stage, seconds in sorted(result.stage_seconds.items()):
        print(f"  {stage}: {seconds:.4f}s")

    synth_seconds = totals[median_idx]
    audio_seconds = len(result.audio) / result.sample_rate
    rtf = synth_seconds / audio_seconds if audio_seconds > 0 else 0.0

    print(f"\nAll-run totals: {[f'{t:.2f}s' for t in totals]}")
    print(f"Median synthesis time: {synth_seconds:.4f}s")
    print(f"Audio duration: {audio_seconds:.4f}s")
    print(f"RTF (median synthesis / audio): {rtf:.4f}x")

    # Check for empty or near-silent audio (RMS < 1e-4)
    rms = compute_rms(result.audio)
    print(f"Audio RMS: {rms:.6f}")

    if len(result.audio) == 0:
        print("FAIL: audio is empty", file=sys.stderr)
        return 1

    if rms < 1e-4:
        print(f"FAIL: audio is near-silent (RMS {rms:.6f} < 1e-4)", file=sys.stderr)
        return 1

    # Write audio to file
    print(f"\nWriting audio to {OUTPUT_PATH} ...")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    sf.write(OUTPUT_PATH, result.audio, result.sample_rate)
    print(f"Wrote {len(result.audio)} samples at {result.sample_rate} Hz")

    print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
