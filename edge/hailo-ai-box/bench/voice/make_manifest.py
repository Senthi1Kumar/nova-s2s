"""Build per-route benchmark manifests from XVF3800 recordings.

Prompted clips come from a `record_asr_corpus.py --corpus speech_v1` directory (its
`prompts.jsonl` stamps `audio_paths["<board>/ch<n>"]`). The board label doubles as the
condition: record with `--board quiet`, `--board music`, `--board meeting`. A label such
as `music-beam` is the music condition on a different route.

Negatives are long recordings that hold no prompted speech (`arecord -c2`), split per
channel and chopped into 10 s clips. Each route gets one `manifest-<label>.json`.
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path
from typing import Any

import numpy as np

RATE = 16000
CLIP_S = 10.0
MIN_TAIL_S = 1.0
CONDITIONS = ("quiet", "music", "meeting")


def read_wav(path: str | Path) -> tuple[int, np.ndarray]:
    """PCM16 WAV as (sample_rate, int16 array of shape (frames, channels))."""
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError(f"{path}: expected PCM16")
        channels = wav.getnchannels()
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        return wav.getframerate(), data.reshape(-1, channels)


def resample(samples: np.ndarray, rate: int) -> np.ndarray:
    if rate == RATE or samples.size == 0:
        return samples
    count = round(samples.size * RATE / rate)
    positions = np.linspace(0, samples.size - 1, count)
    out = np.interp(positions, np.arange(samples.size), samples.astype(np.float64))
    return np.clip(np.rint(out), -32768, 32767).astype("<i2")


def write_mono(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(np.ascontiguousarray(samples, dtype="<i2").tobytes())


def channel_samples(path: str | Path, index: int) -> np.ndarray:
    """One channel at 16 kHz; a mono per-channel file is used whole whatever `index` is."""
    rate, frames = read_wav(path)
    column = frames[:, index] if frames.shape[1] > 1 else frames[:, 0]
    return resample(column, rate)


def condition_of(board: str) -> str | None:
    base = board.split("-")[0]
    return base if base in CONDITIONS else None


def channel_index(channel: str) -> int:
    if not (channel.startswith("ch") and channel[2:].isdigit()):
        raise ValueError(f"channel {channel!r}: expected ch0, ch1, ...")
    return int(channel[2:])


def prompt_entries(
    corpus: Path, out: Path, channel: str, label: str, boards: set[str] | None
) -> list[dict[str, Any]]:
    entries = []
    rows = [json.loads(line) for line in (corpus / "prompts.jsonl").read_text().splitlines() if line.strip()]
    for row in rows:
        for key, relative in sorted((row.get("audio_paths") or {}).items()):
            board, _, chan = key.partition("/")
            if chan != channel:
                continue
            if boards is None:
                # Route-suffixed boards (music-beam) belong to their own route, not ch0/ch1.
                boards_ok = board in CONDITIONS
            else:
                boards_ok = board in boards
            if not boards_ok:
                continue
            condition = condition_of(board)
            if condition is None:
                print(f"skipping unknown board {board!r} (expected quiet, music or meeting)", file=sys.stderr)
                continue
            clip_id = f"{row['id']}__{board}"
            dest = Path(label) / f"{clip_id}.wav"
            write_mono(out / dest, channel_samples(corpus / relative, channel_index(channel)))
            entries.append({
                "id": clip_id, "path": str(dest), "reference": row["reference"],
                "condition": condition, "channel": label, "kind": "prompt",
            })
    return entries


def negative_entries(
    negatives: list[tuple[str, Path]], out: Path, channel: str, label: str
) -> list[dict[str, Any]]:
    entries = []
    clip_len = int(CLIP_S * RATE)
    for condition, path in negatives:
        samples = channel_samples(path, channel_index(channel))
        for n, start in enumerate(range(0, len(samples), clip_len)):
            clip = samples[start:start + clip_len]
            if len(clip) < MIN_TAIL_S * RATE:
                continue
            clip_id = f"neg_{condition}_{path.stem}_{n:03d}"
            dest = Path(label) / f"{clip_id}.wav"
            write_mono(out / dest, clip)
            entries.append({
                "id": clip_id, "path": str(dest), "reference": "", "condition": condition,
                "channel": label, "kind": "negative", "duration_s": round(len(clip) / RATE, 3),
            })
    return entries


def parse_negative(text: str) -> tuple[str, Path]:
    condition, sep, path = text.partition("=")
    if not sep or condition not in CONDITIONS:
        raise argparse.ArgumentTypeError(f"expected CONDITION=PATH with CONDITION in {CONDITIONS}")
    return condition, Path(path)


def build(
    out: Path,
    corpus: Path | None,
    negatives: list[tuple[str, Path]],
    channel: str,
    label: str | None = None,
    boards: set[str] | None = None,
) -> Path:
    label = label or channel
    clips = prompt_entries(corpus, out, channel, label, boards) if corpus else []
    clips += negative_entries(negatives, out, channel, label)
    manifest = out / f"manifest-{label}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"route": label, "clips": clips}, indent=2) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="run directory, e.g. runtime/voice-corpus/<run>")
    parser.add_argument("--corpus", type=Path, help="record_asr_corpus.py speech_v1 directory (prompts.jsonl)")
    parser.add_argument("--negative", action="append", default=[], type=parse_negative,
                        metavar="CONDITION=WAV", help="long recording with no prompted speech; repeatable")
    parser.add_argument("--channel", action="append", help="channel to extract (default: ch0 and ch1)")
    parser.add_argument("--route-label", help="manifest name for a single --channel, e.g. beam")
    parser.add_argument("--boards", help="comma-separated board labels to keep (default: quiet, music and meeting only)")
    args = parser.parse_args(argv)
    channels = args.channel or ["ch0", "ch1"]
    if args.route_label and len(channels) != 1:
        parser.error("--route-label needs exactly one --channel")
    if not args.corpus and not args.negative:
        parser.error("give --corpus and/or --negative")
    boards = set(args.boards.split(",")) if args.boards else None
    for channel in channels:
        manifest = build(args.out, args.corpus, args.negative, channel, args.route_label, boards)
        count = len(json.loads(manifest.read_text())["clips"])
        print(f"{manifest}: {count} clips")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
