"""PCM helpers that keep server audio byte-exact for the voice WebSocket."""

from __future__ import annotations

import io
import wave

import numpy as np
from pipecat.audio.utils import create_file_resampler

SAMPLE_RATE = 16000
# 4 x 10 ms of 16 kHz mono PCM16: the output transport re-chunks to this size.
CHUNK_BYTES = SAMPLE_RATE * 2 * 4 // 100

_resampler = create_file_resampler()


def wav_to_pcm16(wav: bytes) -> tuple[bytes, int]:
    """Decode a PCM16 WAV into mono PCM16LE bytes and its sample rate."""
    with wave.open(io.BytesIO(wav), "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit WAV, got {width * 8}-bit")
    samples = np.frombuffer(frames[: len(frames) // (2 * channels) * 2 * channels], "<i2")
    if channels > 1:
        samples = samples.reshape(-1, channels).astype(np.int32).mean(axis=1).astype("<i2")
    return samples.tobytes(), rate


async def to_16k(pcm: bytes, rate: int) -> bytes:
    if rate == SAMPLE_RATE or not pcm:
        return pcm
    return await _resampler.resample(pcm, rate, SAMPLE_RATE)


def pad_to_chunk(pcm: bytes, chunk: int = CHUNK_BYTES) -> bytes:
    """Zero-pad to a whole number of chunks, staying sample-aligned."""
    if len(pcm) % 2:
        pcm += b"\x00"
    return pcm + b"\x00" * (-len(pcm) % chunk)
