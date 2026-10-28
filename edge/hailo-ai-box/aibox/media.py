"""Image/audio decode + encode helpers. Raises MediaError(status, message)."""

from __future__ import annotations

import io
import shutil
import subprocess
import wave

import numpy as np
from PIL import Image, UnidentifiedImageError

TARGET_SR = 16000
MAX_AUDIO_S = 120.0


class MediaError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def decode_image(data: bytes, max_side: int = 1920) -> np.ndarray:
    """Bytes -> RGB uint8 array, downscaled so the longest side is <= max_side."""
    if not data:
        raise MediaError(400, "empty image upload")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except (UnidentifiedImageError, OSError, SyntaxError) as e:
        raise MediaError(415, f"unsupported or corrupt image: {e}") from e
    except Image.DecompressionBombError as e:
        raise MediaError(413, f"image too large: {e}") from e
    im = im.convert("RGB")
    if max(im.size) > max_side:
        scale = max_side / max(im.size)
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                       Image.BILINEAR)
    return np.asarray(im, dtype=np.uint8)


def _resample(x: np.ndarray, sr: int) -> np.ndarray:
    if sr == TARGET_SR or x.size == 0:
        return x
    if sr > TARGET_SR:  # crude anti-alias: box filter of the decimation ratio
        k = max(1, int(round(sr / TARGET_SR)))
        if k > 1:
            x = np.convolve(x, np.ones(k, np.float32) / k, mode="same")
    n = int(round(x.size * TARGET_SR / sr))
    return np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x).astype(np.float32)


def _decode_wav(data: bytes) -> np.ndarray | None:
    try:
        with wave.open(io.BytesIO(data)) as w:
            ch, width, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (wave.Error, EOFError):
        return None
    if width == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif width == 1:
        x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8)
             | (b[:, 2].astype(np.int8).astype(np.int32) << 16))
        x = v.astype(np.float32) / 8388608.0
    else:
        raise MediaError(415, f"unsupported WAV sample width {width}")
    if ch > 1:
        x = x[: x.size // ch * ch].reshape(-1, ch).mean(axis=1)
    return _resample(x.astype(np.float32), sr)


def _decode_ffmpeg(data: bytes) -> np.ndarray:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise MediaError(415, "only WAV is supported on this host (ffmpeg not installed)")
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar",
             str(TARGET_SR), "-t", str(int(MAX_AUDIO_S) + 1), "-f", "f32le", "pipe:1"],
            input=data, capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        raise MediaError(400, "audio decode timed out") from e
    if r.returncode != 0 or not r.stdout:
        raise MediaError(415, "unsupported or corrupt audio: "
                         + r.stderr.decode("utf-8", "replace")[:200])
    return np.frombuffer(r.stdout, "<f4").astype(np.float32)


def decode_audio(data: bytes) -> np.ndarray:
    """Any supported audio -> float32 16 kHz mono in [-1, 1]."""
    if not data:
        raise MediaError(400, "empty audio upload")
    x = _decode_wav(data) if data[:4] == b"RIFF" else None
    if x is None:
        x = _decode_ffmpeg(data)
    if x.size == 0:
        raise MediaError(400, "audio contains no samples")
    if x.size / TARGET_SR > MAX_AUDIO_S:
        raise MediaError(413, f"audio longer than {MAX_AUDIO_S:.0f}s")
    return x


def _resized(rgb: np.ndarray, width: int) -> Image.Image:
    im = Image.fromarray(rgb)
    if width and im.width != width:
        im = im.resize((width, max(1, round(im.height * width / im.width))), Image.BILINEAR)
    return im


def encode_jpeg(rgb: np.ndarray, width: int, quality: int = 70) -> bytes:
    buf = io.BytesIO()
    _resized(rgb, width).save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def to_rgb565(rgb: np.ndarray, width: int) -> tuple[bytes, int, int]:
    a = np.asarray(_resized(rgb, width), dtype=np.uint16)
    v = ((a[..., 0] & 0xF8) << 8) | ((a[..., 1] & 0xFC) << 3) | (a[..., 2] >> 3)
    return v.astype("<u2").tobytes(), a.shape[1], a.shape[0]
