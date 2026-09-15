#!/usr/bin/env python3
"""Score wake-word clips against openWakeWord (tune threshold on the WM8960).

Record 16 kHz mono WAVs, then:

    arecord -D plughw:2,0 -f S16_LE -r 16000 -c 1 pos/nova_1m_01.wav
    python3 scripts/wakeword_eval.py --pos pos --neg neg --threshold 0.15

Prints max score per clip and FA/FR at the given threshold. Reuses
``OpenWakeWordKWS`` so eval matches the runtime engine.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import soundfile as sf

from nova_hailo.backends.openwakeword_kws import OpenWakeWordKWS, resolve_wakeword_paths


def _load_pcm16(path: Path) -> np.ndarray:
    data, rate = sf.read(str(path), dtype="int16", always_2d=True)
    mono = data.mean(axis=1).astype(np.int16) if data.shape[1] > 1 else data[:, 0]
    if int(rate) != 16000:
        raise SystemExit(f"{path}: expected 16 kHz, got {rate}")
    return mono


def _iter_wavs(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        p for p in folder.iterdir() if p.suffix.lower() in {".wav", ".wave"} and p.is_file()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Eval Nova openWakeWord clips.")
    parser.add_argument("--pos", type=Path, default=Path("pos"), help="Positive WAVs (contain Nova)")
    parser.add_argument("--neg", type=Path, default=Path("neg"), help="Negative WAVs (no Nova)")
    parser.add_argument("--threshold", type=float, default=0.15)
    parser.add_argument("--model", default="models/wakeword/nova_pro_v3.onnx")
    args = parser.parse_args()

    paths = resolve_wakeword_paths(root=ROOT, model_path=args.model)
    if paths is None:
        print("wake-word models missing — run ./scripts/fetch_models.sh", file=sys.stderr)
        return 2
    engine = OpenWakeWordKWS(
        model_path=paths.model,
        melspec_path=paths.melspec,
        embedding_path=paths.embedding,
        threshold=args.threshold,
        consecutive_hits=1,
        cooldown_s=0.0,
    )
    print(f"model     : {paths.model}")
    print(f"threshold : {args.threshold:.3f}")

    def run_dir(label: str, folder: Path, expect_hit: bool) -> tuple[int, int]:
        wavs = _iter_wavs(folder)
        if not wavs:
            print(f"\n{label}: (no wavs in {folder})")
            return 0, 0
        print(f"\n{label} ({folder}):")
        n_err = 0
        for wav in wavs:
            peak = engine.score_clip(_load_pcm16(wav))
            hit = peak >= args.threshold
            wrong = hit != expect_hit
            if wrong:
                n_err += 1
            mark = "HIT" if hit else "miss"
            flag = "  <-- error" if wrong else ""
            print(f"  {wav.name:40s}  max={peak:.4f}  {mark}{flag}")
        return len(wavs), n_err

    n_pos, fr = run_dir("pos", args.pos, True)
    n_neg, fa = run_dir("neg", args.neg, False)
    print("\n---")
    if n_pos:
        print(f"FR  {fr}/{n_pos}  ({fr / n_pos:.1%})")
    else:
        print("FR  n/a (no positive clips)")
    if n_neg:
        print(f"FA  {fa}/{n_neg}  ({fa / n_neg:.1%})")
    else:
        print("FA  n/a (no negative clips)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
