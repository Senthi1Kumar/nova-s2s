#!/usr/bin/env python3
"""Record a real ASR corpus on the target Pi (WM8960), replacing TTS resynthesis.

WER measured against Piper-resynthesised audio is an instrument check, not a
quality claim: it never exercises the microphone, room, or far-field path. This
records the operator actually speaking each reference phrase, so every ASR
candidate in the model matrix is scored on the same real audio.

Usage (on the Pi, from ~/nsk/nova-hailo):
    python3 scripts/record_asr_corpus.py                 # record all pending
    python3 scripts/record_asr_corpus.py --redo asr_003  # re-record one
    python3 scripts/record_asr_corpus.py --distance far  # tag the condition

Writes bench/corpus/fixtures/audio/<id>__<distance>.wav (16 kHz mono) and stamps
`audio_path` into bench/corpus/asr_transcripts.jsonl.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "bench" / "corpus"
MANIFEST = CORPUS / "asr_transcripts.jsonl"
AUDIO_DIR = CORPUS / "fixtures" / "audio"
SAMPLE_RATE = 16000
MAX_SECONDS = 8.0
SILENCE_TAIL_S = 0.9
START_RMS = 0.02


def read_manifest() -> list[dict]:
    rows = []
    with MANIFEST.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_manifest(rows: list[dict]) -> None:
    with MANIFEST.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def record_until_silence(device) -> np.ndarray:
    """Record until ~0.9s of silence after speech starts, or MAX_SECONDS."""
    frames: list[np.ndarray] = []
    block = int(SAMPLE_RATE * 0.05)
    started = False
    silent_blocks = 0
    needed_silent = int(SILENCE_TAIL_S / 0.05)
    max_blocks = int(MAX_SECONDS / 0.05)

    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", device=device, blocksize=block
    ) as stream:
        # Discard the first ~250ms: the Enter keypress lands here as a full-scale
        # click, which showed up as peak=1.000 with rms=0.024 across most takes
        # (measured 2026-07-30) and would train the gate on a transient.
        for _ in range(int(0.25 / 0.05)):
            try:
                stream.read(block)
            except Exception:
                break
        for _ in range(max_blocks):
            buf, _overflow = stream.read(block)
            mono = np.asarray(buf, dtype=np.float32).reshape(-1)
            frames.append(mono)
            rms = float(np.sqrt(np.mean(mono**2)) + 1e-12)
            if rms >= START_RMS:
                started = True
                silent_blocks = 0
            elif started:
                silent_blocks += 1
                if silent_blocks >= needed_silent:
                    break
    return np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--redo",
        nargs="?",
        const="all",
        default="",
        help="'all', or comma-separated case ids, to re-record (bare --redo means all)",
    )
    ap.add_argument("--distance", default="near", help="near | mid | far (tags the file)")
    ap.add_argument("--device", default=None, help="input device index or name")
    ap.add_argument(
        "--no-mic-gain",
        action="store_true",
        help="skip the WM8960 gain/ALC setup (applied by default)",
    )
    args = ap.parse_args()

    # Mixer state does not survive a reboot, and a corpus captured at the wrong
    # input level is silently useless -- it was worth 0.167 vs 0.123 near WER.
    # Apply it here too so recording never depends on remembering a second step.
    if not args.no_mic_gain:
        gain = Path(__file__).resolve().parent / "set_wm8960_mic_gain.sh"
        if gain.is_file():
            subprocess.run([str(gain), "100"], check=False)

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    rows = read_manifest()
    redo_all = args.redo.strip().lower() == "all"
    redo = set() if redo_all else {s.strip() for s in args.redo.split(",") if s.strip()}

    device = args.device
    if device is not None and str(device).isdigit():
        device = int(device)
    if device is None:
        from nova_hailo.config import resolve_audio_device_ids

        device, _out = resolve_audio_device_ids("wm8960", "wm8960")

    print(f"Recording at {SAMPLE_RATE} Hz, input device={device!r}, distance={args.distance}")
    print("Speak each phrase naturally after the prompt. Ctrl-C to stop early.\n")

    recorded = 0
    for row in rows:
        cid = row["id"]
        already = (row.get("audio_paths") or {}).get(args.distance)
        if already and not redo_all and cid not in redo:
            continue
        dest = AUDIO_DIR / f"{cid}__{args.distance}.wav"
        print(f"[{cid}] SAY:  {row['reference']}")
        input("        press Enter, then speak... ")
        audio = record_until_silence(device)
        dur = len(audio) / SAMPLE_RATE
        rms = float(np.sqrt(np.mean(audio**2)) + 1e-12) if audio.size else 0.0
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if dur < 0.3 or rms < 0.01:
            print(f"        REJECTED (dur={dur:.2f}s rms={rms:.4f}) - retry with --redo {cid}\n")
            continue
        sf.write(str(dest), audio, SAMPLE_RATE)
        rel = str(dest.relative_to(CORPUS))
        # Keep one entry per distance so near/far coexist; audio_path stays as the
        # default the runner picks when no distance is requested.
        paths = dict(row.get("audio_paths") or {})
        paths[args.distance] = rel
        row["audio_paths"] = paths
        row["audio_path"] = paths.get("near") or rel
        row["distance"] = args.distance
        recorded += 1
        # Actionable level feedback: ASR wants healthy average level and headroom.
        # Whisper's own skip floor is rms<0.015, and clipping destroys accuracy
        # faster than low level does.
        flags = []
        if peak >= 0.99:
            flags.append("CLIPPED - lower mic gain or back off the mic")
        if rms < 0.03:
            flags.append("QUIET - speak up (target rms 0.05-0.15)")
        if 0.05 <= rms <= 0.15 and peak < 0.95:
            flags.append("good level")
        note = ("  [" + "; ".join(flags) + "]") if flags else ""
        print(f"        saved {dest.name}  dur={dur:.2f}s rms={rms:.3f} peak={peak:.3f}{note}\n")

    write_manifest(rows)
    have = sum(1 for r in rows if r.get("audio_path"))
    print(f"Recorded {recorded} this run. Corpus now {have}/{len(rows)} with real audio.")
    print("Re-run the matrix ASR lane to score every candidate on it.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted - manifest not rewritten for pending rows")
        sys.exit(130)
