"""CPU speech-to-text via parakeet.cpp's C API (libparakeet.so, ABI >= 5).

Selected with ``model.stt_engine: parakeet``. Measured on the target Pi against a
recorded corpus of real microphone audio, parakeet-tdt_ctc-110m q8_0 halves
Whisper-Base's near-field WER (0.167 vs 0.333), thirds it at distance
(0.20 vs 0.60), runs faster on four CPU cores than Whisper-Base does on the
Hailo NPU (292 ms vs 344 ms), and is the only engine tested that transcribes the
wake word "Nova". Running STT on CPU also leaves the NPU entirely to the LLM,
which is the bandwidth-bound stage.

Interface matches ``WhisperSTT``: ``transcribe(audio) -> str`` and ``release()``.
"""
from __future__ import annotations

import ctypes
import os
import re
from pathlib import Path

import numpy as np

DECODER_DEFAULT = 0
DECODER_CTC = 1
DECODER_TDT = 2

_DECODERS = {"ctc": DECODER_CTC, "tdt": DECODER_TDT, "rnnt": DECODER_TDT}

# parakeet output is far cleaner than Whisper's, but a stray bracketed token must
# never reach the LLM prompt or the UI.
_CONTROL_RE = re.compile(r"<\|[^|>]{0,24}\|>")


def _find_first(paths: list[Path]) -> Path | None:
    for p in paths:
        if p.is_file():
            return p
    return None


class ParakeetSTT:
    """Resident parakeet.cpp model; transcribes in-memory 16 kHz mono float32."""

    def __init__(
        self,
        gguf_path: str | Path,
        lib_path: str | Path,
        *,
        decoder: str | int = DECODER_DEFAULT,
        language: str | None = None,
    ):
        self._lib = ctypes.CDLL(str(lib_path))
        self._bind()
        abi = self._lib.parakeet_capi_abi_version()
        if abi < 5:
            raise RuntimeError(f"libparakeet ABI {abi} too old (need >= 5)")
        self.abi_version = abi
        if isinstance(decoder, int):
            self.decoder = decoder
        else:
            self.decoder = _DECODERS.get(str(decoder).lower(), DECODER_DEFAULT)
        self.language = language
        self._ctx = self._lib.parakeet_capi_load(str(gguf_path).encode())
        if not self._ctx:
            raise RuntimeError(f"parakeet_capi_load failed for {gguf_path}: {self._last_error()}")
        self.model_path = str(gguf_path)
        self.hef_path = str(gguf_path)  # parity with WhisperSTT logging

    def _bind(self) -> None:
        lib = self._lib
        lib.parakeet_capi_abi_version.restype = ctypes.c_int
        lib.parakeet_capi_load.restype = ctypes.c_void_p
        lib.parakeet_capi_load.argtypes = [ctypes.c_char_p]
        lib.parakeet_capi_free.argtypes = [ctypes.c_void_p]
        lib.parakeet_capi_free_string.argtypes = [ctypes.c_void_p]
        lib.parakeet_capi_last_error.restype = ctypes.c_char_p
        lib.parakeet_capi_last_error.argtypes = [ctypes.c_void_p]
        base = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        lib.parakeet_capi_transcribe_pcm.restype = ctypes.c_void_p
        lib.parakeet_capi_transcribe_pcm.argtypes = base
        lib.parakeet_capi_transcribe_pcm_lang.restype = ctypes.c_void_p
        lib.parakeet_capi_transcribe_pcm_lang.argtypes = base + [ctypes.c_char_p]

    def _last_error(self) -> str:
        try:
            err = self._lib.parakeet_capi_last_error(self._ctx)
            return err.decode("utf-8", "replace") if err else "unknown"
        except Exception:
            return "unknown"

    def _take_string(self, ptr) -> str:
        if not ptr:
            raise RuntimeError(f"parakeet transcribe failed: {self._last_error()}")
        try:
            val = ctypes.cast(ptr, ctypes.c_char_p).value
            return (val or b"").decode("utf-8", "replace")
        finally:
            self._lib.parakeet_capi_free_string(ctypes.c_void_p(ptr))

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> str:
        x = np.ascontiguousarray(np.asarray(audio, dtype=np.float32).reshape(-1))
        if x.size < 1600:  # <0.1s
            return ""
        ptr = x.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        lang = language if language is not None else self.language
        if lang:
            raw = self._lib.parakeet_capi_transcribe_pcm_lang(
                self._ctx, ptr, x.size, 16000, self.decoder, str(lang).encode()
            )
        else:
            raw = self._lib.parakeet_capi_transcribe_pcm(
                self._ctx, ptr, x.size, 16000, self.decoder
            )
        text = _CONTROL_RE.sub(" ", self._take_string(raw))
        return re.sub(r"\s+", " ", text).strip()

    def release(self) -> None:
        if getattr(self, "_ctx", None):
            self._lib.parakeet_capi_free(ctypes.c_void_p(self._ctx))
            self._ctx = None


def resolve_parakeet_paths(
    model: str | None = None, lib: str | None = None
) -> tuple[Path | None, Path | None]:
    """Locate the gguf and libparakeet.so, honouring env overrides."""
    root = Path(__file__).resolve().parent.parent.parent
    bench = root.parent / "asr-bench"
    model_cands = [Path(p) for p in (model, os.environ.get("NOVA_PARAKEET_MODEL")) if p]
    # f16 first: upstream recommends it, and our own A/B produced identical
    # near-field transcripts ~14% faster than q8_0. q8_0 stays as a fallback for
    # kits that only shipped the smaller file.
    model_cands += [
        root / "models" / "parakeet" / "tdt_ctc-110m-f16.gguf",
        root / "models" / "parakeet" / "tdt_ctc-110m-q8_0.gguf",
        bench / "models" / "tdt_ctc-110m-f16.gguf",
        bench / "models" / "tdt_ctc-110m-q8_0.gguf",
    ]
    lib_cands = [Path(p) for p in (lib, os.environ.get("NOVA_PARAKEET_LIB")) if p]
    lib_cands += [
        root / "models" / "parakeet" / "libparakeet.so",
        bench / "parakeet" / "parakeet-v0.4.0-lib-linux-cpu-arm64" / "libparakeet.so",
    ]
    return _find_first(model_cands), _find_first(lib_cands)
