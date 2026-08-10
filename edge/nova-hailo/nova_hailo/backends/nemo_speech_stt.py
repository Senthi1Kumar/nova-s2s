"""CPU speech-to-text via NeMo-Speech.cpp's C API (libnemo_speech_asr_c.so).

Selected with ``model.stt_engine: nemo_speech``. NVIDIA's native ggml/GGUF ASR
runtime (nemotron-speech-streaming-en-0.6b, cache-aware FastConformer-RNNT),
evaluated as a Sprint 8 A/B candidate against parakeet.cpp. Struct layout and
call sequence mirror include/nemo_speech/asr.h and docs/sdk.md's "Minimal
offline ASR" example exactly, since the size-prefixed structs are the ABI
contract.

Interface matches ``ParakeetSTT``: ``transcribe(audio) -> str`` and ``release()``.
"""
from __future__ import annotations

import ctypes
import os
import re
from pathlib import Path

import numpy as np

_OK = 0  # NEMO_SPEECH_ASR_OK

# The runtime can leave stray control tokens in a transcript, same risk as
# parakeet.cpp's output.
_CONTROL_RE = re.compile(r"<\|[^|>]{0,24}\|>")


class _BackendConfig(ctypes.Structure):
    _fields_ = [("size", ctypes.c_size_t), ("gpu", ctypes.c_int32)]


class _ModelConfig(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("path", ctypes.c_char_p),
        ("name", ctypes.c_char_p),
    ]


class _StreamingConfig(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("chunk_size", ctypes.c_float),
        ("ctc_left_padding", ctypes.c_float),
        ("ctc_right_padding", ctypes.c_float),
        ("rnnt_right_context", ctypes.c_int32),
    ]


class _VadConfig(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("model_path", ctypes.c_char_p),
        ("enable_masking", ctypes.c_bool),
        ("onset", ctypes.c_float),
        ("offset", ctypes.c_float),
    ]


class _EndpointingConfig(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("enable", ctypes.c_bool),
        ("vad_based", ctypes.c_bool),
        ("stop_history_eou_ms", ctypes.c_int32),
    ]


class _RecognizerConfig(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("backend", ctypes.POINTER(_BackendConfig)),
        ("model", ctypes.POINTER(_ModelConfig)),
        ("streaming", ctypes.POINTER(_StreamingConfig)),
        ("decoder", ctypes.c_void_p),
        ("vad", ctypes.POINTER(_VadConfig)),
        ("endpointing", ctypes.POINTER(_EndpointingConfig)),
        ("postproc", ctypes.c_void_p),
        ("diar", ctypes.c_void_p),
        ("batching", ctypes.c_void_p),
    ]


class _SpeechContext(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("phrases", ctypes.POINTER(ctypes.c_char_p)),
        ("phrase_count", ctypes.c_size_t),
        ("boost", ctypes.c_float),
    ]


class _RecognitionOptions(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("request_id", ctypes.c_char_p),
        ("language_code", ctypes.c_char_p),
        ("interim_results", ctypes.c_bool),
        ("enable_word_time_offsets", ctypes.c_bool),
        ("enable_automatic_punctuation", ctypes.c_bool),
        ("verbatim_transcripts", ctypes.c_bool),
        ("profanity_filter", ctypes.c_bool),
        ("stop_history_eou_ms", ctypes.c_int32),
        ("speech_contexts", ctypes.POINTER(_SpeechContext)),
        ("speech_context_count", ctypes.c_size_t),
        ("max_alternatives", ctypes.c_int32),
        ("enable_speaker_diarization", ctypes.c_bool),
        ("max_speaker_count", ctypes.c_int32),
    ]


def _find_first(paths: list[Path]) -> Path | None:
    for p in paths:
        if p.is_file():
            return p
    return None


class NemoSpeechSTT:
    """Resident NeMo-Speech.cpp model; transcribes in-memory 16 kHz mono float32."""

    def __init__(
        self,
        gguf_path: str | Path,
        lib_path: str | Path,
        *,
        decoder: str | int = 0,
        language: str | None = None,
        vad_model: str | Path | None = None,
    ):
        self._lib = ctypes.CDLL(str(lib_path))
        self._bind()
        self.language = language
        self.model_path = str(gguf_path)
        self.hef_path = str(gguf_path)  # parity with WhisperSTT/ParakeetSTT logging

        # Config structs are copied by nemo_speech_asr_create, but keep them
        # referenced on self until after that call so ctypes can't collect the
        # backing memory of the pointer fields first.
        self._model_cfg = _ModelConfig(
            size=ctypes.sizeof(_ModelConfig), path=self.model_path.encode(), name=None
        )
        self._backend_cfg = _BackendConfig(size=ctypes.sizeof(_BackendConfig), gpu=-1)
        rec_cfg = _RecognizerConfig(
            size=ctypes.sizeof(_RecognizerConfig),
            backend=ctypes.pointer(self._backend_cfg),
            model=ctypes.pointer(self._model_cfg),
        )
        if vad_model:
            # mask_enable defaults false even with a model loaded (docs/asr/
            # configuration.md) -- floors silence mel frames before the
            # encoder, which is what a far-field empty transcript needs.
            self._vad_cfg = _VadConfig(
                size=ctypes.sizeof(_VadConfig),
                model_path=str(vad_model).encode(),
                enable_masking=True,
                onset=0.5,
                offset=0.3,
            )
            rec_cfg.vad = ctypes.pointer(self._vad_cfg)
        self._ctx = ctypes.c_void_p()
        status = self._lib.nemo_speech_asr_create(ctypes.byref(rec_cfg), ctypes.byref(self._ctx))
        if status != _OK or not self._ctx:
            raise RuntimeError(
                f"nemo_speech_asr_create failed for {gguf_path}: {self._last_error()}"
            )

    def _bind(self) -> None:
        lib = self._lib
        lib.nemo_speech_asr_create.restype = ctypes.c_int
        lib.nemo_speech_asr_create.argtypes = [
            ctypes.POINTER(_RecognizerConfig),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.nemo_speech_asr_destroy.argtypes = [ctypes.c_void_p]
        lib.nemo_speech_asr_recognition_options_default.restype = _RecognitionOptions
        lib.nemo_speech_asr_recognize_f32.restype = ctypes.c_int
        lib.nemo_speech_asr_recognize_f32.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_RecognitionOptions),
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.nemo_speech_asr_result_alternative_count.restype = ctypes.c_size_t
        lib.nemo_speech_asr_result_alternative_count.argtypes = [ctypes.c_void_p]
        lib.nemo_speech_asr_result_transcript.restype = ctypes.c_char_p
        lib.nemo_speech_asr_result_transcript.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        lib.nemo_speech_asr_result_is_final.restype = ctypes.c_bool
        lib.nemo_speech_asr_result_is_final.argtypes = [ctypes.c_void_p]
        lib.nemo_speech_asr_result_destroy.argtypes = [ctypes.c_void_p]
        lib.nemo_speech_asr_last_error.restype = ctypes.c_char_p

        lib.nemo_speech_asr_streaming_recognize.restype = ctypes.c_int
        lib.nemo_speech_asr_streaming_recognize.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_RecognitionOptions),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.nemo_speech_asr_stream_push_f32.restype = ctypes.c_int
        lib.nemo_speech_asr_stream_push_f32.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
            ctypes.c_int32,
        ]
        lib.nemo_speech_asr_stream_finish.restype = ctypes.c_int
        lib.nemo_speech_asr_stream_finish.argtypes = [ctypes.c_void_p]
        lib.nemo_speech_asr_stream_next.restype = ctypes.c_int
        lib.nemo_speech_asr_stream_next.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.nemo_speech_asr_stream_close.argtypes = [ctypes.c_void_p]

    def _last_error(self) -> str:
        try:
            err = self._lib.nemo_speech_asr_last_error()
            return err.decode("utf-8", "replace") if err else "unknown"
        except Exception:
            return "unknown"

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> str:
        x = np.ascontiguousarray(np.asarray(audio, dtype=np.float32).reshape(-1))
        if x.size < 1600:  # <0.1s
            return ""
        options = self._lib.nemo_speech_asr_recognition_options_default()
        lang = language if language is not None else self.language
        lang_bytes = str(lang).encode() if lang else None
        options.language_code = lang_bytes
        ptr = x.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        result = ctypes.c_void_p()
        status = self._lib.nemo_speech_asr_recognize_f32(
            self._ctx, ctypes.byref(options), ptr, x.size, 16000, ctypes.byref(result)
        )
        if status != _OK or not result:
            raise RuntimeError(f"nemo_speech transcribe failed: {self._last_error()}")
        try:
            if self._lib.nemo_speech_asr_result_alternative_count(result) == 0:
                return ""
            raw = self._lib.nemo_speech_asr_result_transcript(result, 0)
            text = _CONTROL_RE.sub(" ", (raw or b"").decode("utf-8", "replace"))
            return re.sub(r"\s+", " ", text).strip()
        finally:
            self._lib.nemo_speech_asr_result_destroy(result)

    def release(self) -> None:
        if getattr(self, "_ctx", None) and self._ctx.value:
            self._lib.nemo_speech_asr_destroy(self._ctx)
            self._ctx = None

    # ---- Streaming: overlap decode with capture instead of decoding the
    # whole utterance after speech ends. push_f32() only buffers -- stream_next()
    # is what drives decode work forward, so a caller must poll it after every
    # push to get the overlap; polling only at finish() is equivalent to the
    # offline path. ----

    def open_stream(self, language: str | None = None) -> ctypes.c_void_p:
        options = self._lib.nemo_speech_asr_recognition_options_default()
        lang = language if language is not None else self.language
        lang_bytes = str(lang).encode() if lang else None
        options.language_code = lang_bytes
        self._stream_lang_bytes = lang_bytes  # keep alive for the options struct's lifetime
        stream = ctypes.c_void_p()
        status = self._lib.nemo_speech_asr_streaming_recognize(
            self._ctx, ctypes.byref(options), ctypes.byref(stream)
        )
        if status != _OK or not stream:
            raise RuntimeError(f"nemo_speech stream open failed: {self._last_error()}")
        return stream

    def push_stream(self, stream: ctypes.c_void_p, audio: np.ndarray) -> None:
        x = np.ascontiguousarray(np.asarray(audio, dtype=np.float32).reshape(-1))
        if x.size == 0:
            return
        ptr = x.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        status = self._lib.nemo_speech_asr_stream_push_f32(stream, ptr, x.size, 16000)
        if status != _OK:
            raise RuntimeError(f"nemo_speech stream push failed: {self._last_error()}")

    def poll_stream(self, stream: ctypes.c_void_p) -> tuple[str, bool] | None:
        """Drive decode forward. Returns (text, is_final) or None if nothing is ready yet."""
        result = ctypes.c_void_p()
        status = self._lib.nemo_speech_asr_stream_next(stream, ctypes.byref(result))
        if status != _OK:
            raise RuntimeError(f"nemo_speech stream poll failed: {self._last_error()}")
        if not result:
            return None
        try:
            if self._lib.nemo_speech_asr_result_alternative_count(result) == 0:
                return ("", bool(self._lib.nemo_speech_asr_result_is_final(result)))
            raw = self._lib.nemo_speech_asr_result_transcript(result, 0)
            text = _CONTROL_RE.sub(" ", (raw or b"").decode("utf-8", "replace"))
            text = re.sub(r"\s+", " ", text).strip()
            is_final = bool(self._lib.nemo_speech_asr_result_is_final(result))
            return (text, is_final)
        finally:
            self._lib.nemo_speech_asr_result_destroy(result)

    def finish_stream(self, stream: ctypes.c_void_p) -> None:
        status = self._lib.nemo_speech_asr_stream_finish(stream)
        if status != _OK:
            raise RuntimeError(f"nemo_speech stream finish failed: {self._last_error()}")

    def close_stream(self, stream: ctypes.c_void_p) -> None:
        self._lib.nemo_speech_asr_stream_close(stream)


def resolve_nemo_speech_paths(
    model: str | None = None, lib: str | None = None
) -> tuple[Path | None, Path | None]:
    """Locate the gguf and libnemo_speech_asr_c.so, honouring env overrides."""
    root = Path(__file__).resolve().parent.parent.parent
    model_cands = [Path(p) for p in (model, os.environ.get("NOVA_NEMO_SPEECH_MODEL")) if p]
    model_cands += [
        root / "models" / "nemo_speech" / "nemotron-speech-streaming-en-0.6b.q8_0.gguf",
    ]
    lib_cands = [Path(p) for p in (lib, os.environ.get("NOVA_NEMO_SPEECH_LIB")) if p]
    lib_cands += [
        root / "models" / "nemo_speech" / "libnemo_speech_asr_c.so",
    ]
    return _find_first(model_cands), _find_first(lib_cands)


def resolve_nemo_speech_vad_path(vad: str | None = None) -> Path | None:
    """Locate the Silero VAD GGUF used for feature masking (optional)."""
    root = Path(__file__).resolve().parent.parent.parent
    cands = [Path(p) for p in (vad, os.environ.get("NOVA_NEMO_SPEECH_VAD")) if p]
    cands += [root / "models" / "nemo_speech" / "silero-v6.2.0.gguf"]
    return _find_first(cands)
