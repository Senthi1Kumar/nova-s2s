"""Matrix-only ASR adapters for CPU engines.

Caller: scripts/run_model_matrix.py::run_asr_candidate / _run_cpu_asr_candidate.
Existing file — extended beyond parakeet.cpp for Cohere/Moonshine/Nemotron bakeoffs.

Adapters expose the same contract as WhisperSTT:

    transcribe(audio: np.ndarray[float32, 16k mono]) -> str
    release() -> None

parakeet.cpp uses libparakeet.so (persistent load). Cohere GGUF uses
transcribe.cpp CLI (reload per utt — WER-first). Moonshine uses transformers.
Nemotron ONNX INT4 uses onnxruntime-genai.
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

# decoder selector for parakeet_capi_transcribe_pcm
DECODER_DEFAULT = 0
DECODER_CTC = 1
DECODER_TDT = 2


class ParakeetASR:
    """libparakeet.so C-API binding (ABI >= 5)."""

    def __init__(
        self,
        lib_path: str | Path,
        gguf_path: str | Path,
        *,
        decoder: int = DECODER_DEFAULT,
        language: str | None = None,
    ):
        self._lib = ctypes.CDLL(str(lib_path))
        self._bind()
        abi = self._lib.parakeet_capi_abi_version()
        if abi < 5:
            raise RuntimeError(f"libparakeet ABI {abi} too old (need >= 5)")
        self.abi_version = abi
        self.decoder = int(decoder)
        # Multilingual (nemotron) models take a language prompt; "" -> model default.
        self.language = language
        self._ctx = self._lib.parakeet_capi_load(str(gguf_path).encode())
        if not self._ctx:
            raise RuntimeError(f"parakeet_capi_load failed for {gguf_path}: {self._last_error()}")
        self.model_path = str(gguf_path)

    def _bind(self) -> None:
        lib = self._lib
        lib.parakeet_capi_abi_version.restype = ctypes.c_int
        lib.parakeet_capi_load.restype = ctypes.c_void_p
        lib.parakeet_capi_load.argtypes = [ctypes.c_char_p]
        lib.parakeet_capi_free.argtypes = [ctypes.c_void_p]
        lib.parakeet_capi_free_string.argtypes = [ctypes.c_void_p]
        lib.parakeet_capi_last_error.restype = ctypes.c_char_p
        lib.parakeet_capi_last_error.argtypes = [ctypes.c_void_p]
        lib.parakeet_capi_transcribe_pcm.restype = ctypes.c_void_p
        lib.parakeet_capi_transcribe_pcm.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        lib.parakeet_capi_transcribe_pcm_lang.restype = ctypes.c_void_p
        lib.parakeet_capi_transcribe_pcm_lang.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
        ]

    def _last_error(self) -> str:
        try:
            err = self._lib.parakeet_capi_last_error(self._ctx)
            return err.decode("utf-8", "replace") if err else "unknown"
        except Exception:
            return "unknown"

    def _take_string(self, ptr: int | None) -> str:
        """Copy a malloc'd C string then free it via the library's allocator."""
        if not ptr:
            raise RuntimeError(f"parakeet transcribe failed: {self._last_error()}")
        try:
            return ctypes.cast(ptr, ctypes.c_char_p).value.decode("utf-8", "replace")
        finally:
            self._lib.parakeet_capi_free_string(ctypes.c_void_p(ptr))

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> str:
        x = np.ascontiguousarray(np.asarray(audio, dtype=np.float32).reshape(-1))
        if x.size == 0:
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
        return self._take_string(raw).strip()

    def release(self) -> None:
        if getattr(self, "_ctx", None):
            self._lib.parakeet_capi_free(ctypes.c_void_p(self._ctx))
            self._ctx = None


class TranscribeCppASR:
    """Cohere GGUF via handy-computer/transcribe.cpp CLI (WER bakeoff)."""

    def __init__(
        self,
        cli_path: str | Path,
        gguf_path: str | Path,
        *,
        language: str | None = "en",
        threads: int = 4,
    ):
        self.cli = Path(cli_path)
        self.model_path = Path(gguf_path)
        self.language = language
        self.threads = int(threads)
        if not self.cli.is_file():
            raise FileNotFoundError(f"transcribe-cli not found: {self.cli}")
        if not self.model_path.is_file():
            raise FileNotFoundError(f"gguf not found: {self.model_path}")

    @staticmethod
    def _parse_cli_transcript(stdout: str) -> str:
        """Extract hypothesis from transcribe-cli stdout.

        Prefer ``text: ...``; otherwise the last non-banner non-empty line.
        Banner noise includes audio/samples/realtime/license/run/usage lines.
        """
        lines = [ln.rstrip() for ln in (stdout or "").splitlines()]
        for ln in lines:
            m = re.match(r"^text:\s*(.*)$", ln, flags=re.IGNORECASE)
            if m:
                return m.group(1).strip()
        banner_needles = (
            "samples:",
            "realtime:",
            "license:",
            "max audio:",
            "run:",
            "usage:",
        )
        for ln in reversed(lines):
            s = ln.strip()
            if not s:
                continue
            low = s.lower()
            if any(n in low for n in banner_needles):
                continue
            return s
        return ""

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> str:
        import soundfile as sf

        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return ""
        lang = language if language is not None else self.language
        with tempfile.TemporaryDirectory(prefix="nova_asr_") as td:
            wav = Path(td) / "utt.wav"
            sf.write(str(wav), x, 16000, subtype="PCM_16")
            # Note: -t is --translate in transcribe.cpp; threads is --threads.
            # Args: -m MODEL -l LANG --threads N -q WAV
            cmd = [
                str(self.cli),
                "-m",
                str(self.model_path),
                "--threads",
                str(self.threads),
                "-q",
                str(wav),
            ]
            if lang:
                cmd[3:3] = ["-l", str(lang)]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(f"transcribe-cli failed ({proc.returncode}): {err[:400]}")
            return self._parse_cli_transcript(proc.stdout or "")

    def release(self) -> None:
        return None


class TransformersAsr:
    """Moonshine Streaming / HF seq2seq ASR on CPU."""

    def __init__(self, model_id: str, *, language: str | None = "en", device: str = "cpu"):
        import torch
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

        self.device = device
        self.language = language
        self.model_id = model_id
        try:
            from transformers import MoonshineStreamingForConditionalGeneration

            self.model = MoonshineStreamingForConditionalGeneration.from_pretrained(
                model_id
            ).to(device)
        except Exception:
            self.model = AutoModelForSpeechSeq2Seq.from_pretrained(model_id).to(device)
        self.model.eval()
        self.processor = AutoProcessor.from_pretrained(model_id)
        self._torch = torch

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> str:
        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return ""
        sr = getattr(self.processor.feature_extractor, "sampling_rate", 16000)
        inputs = self.processor(x, return_tensors="pt", sampling_rate=sr)
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        token_limit_factor = 6.5 / float(sr)
        seq_lens = inputs["attention_mask"].sum(dim=-1)
        max_length = max(8, int((seq_lens * token_limit_factor).max().item()))
        with self._torch.inference_mode():
            ids = self.model.generate(**inputs, max_length=max_length)
        return self.processor.decode(ids[0], skip_special_tokens=True).strip()

    def release(self) -> None:
        self.model = None
        self.processor = None


class OnnxRuntimeGenaiAsr:
    """Nemotron ONNX INT4 via onnxruntime-genai."""

    def __init__(self, model_dir: str | Path, *, language: str | None = "en-US"):
        import onnxruntime_genai as og

        self.model_dir = Path(model_dir)
        self.language = language
        self._og = og
        self.model = og.Model(str(self.model_dir))
        self.tokenizer = og.Tokenizer(self.model)

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> str:
        import soundfile as sf

        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return ""
        lang = language if language is not None else self.language
        with tempfile.TemporaryDirectory(prefix="nova_oga_") as td:
            wav = Path(td) / "utt.wav"
            sf.write(str(wav), x, 16000, subtype="PCM_16")
            try:
                processor = self._og.MultiModalProcessor(self.model)
                prompts = [f"<|audio|>{lang or 'en-US'}"]
                inputs = processor(prompts, [str(wav)])
                params = self._og.GeneratorParams(self.model)
                params.set_inputs(inputs)
                generator = self._og.Generator(self.model, params)
                while not generator.is_done():
                    generator.generate_next_token()
                return self.tokenizer.decode(generator.get_sequence(0)).strip()
            except Exception as exc:
                raise RuntimeError(
                    f"onnxruntime_genai ASR path failed for {self.model_dir}: {exc}"
                ) from exc

    def release(self) -> None:
        self.model = None
        self.tokenizer = None


def create_parakeet(candidate: dict, artifact: Path) -> ParakeetASR:
    """Build a ParakeetASR from a manifest candidate row."""
    lib = candidate.get("lib_path") or "libparakeet.so"
    decoder = {
        "ctc": DECODER_CTC,
        "tdt": DECODER_TDT,
        "rnnt": DECODER_TDT,
    }.get(str(candidate.get("decoder") or "").lower(), DECODER_DEFAULT)
    return ParakeetASR(lib, artifact, decoder=decoder, language=candidate.get("language"))


def create_asr_engine(candidate: dict[str, Any], artifact: Path | None):
    """Factory for matrix ASR backends."""
    backend = str(candidate.get("backend") or "")
    if backend == "parakeet_capi":
        if artifact is None:
            raise FileNotFoundError("gguf artifact not found")
        return create_parakeet(candidate, artifact)
    if backend == "transcribe_cpp":
        if artifact is None:
            raise FileNotFoundError("gguf artifact not found")
        cli = (
            candidate.get("cli_path")
            or os.environ.get("NOVA_TRANSCRIBE_CLI")
            or "transcribe-cli"
        )
        return TranscribeCppASR(
            cli,
            artifact,
            language=candidate.get("language") or "en",
            threads=int(candidate.get("threads") or 4),
        )
    if backend == "transformers_asr":
        model_id = candidate.get("hf_id") or (str(artifact) if artifact else None)
        if not model_id:
            raise FileNotFoundError("transformers ASR model id/path missing")
        return TransformersAsr(str(model_id), language=candidate.get("language") or "en")
    if backend == "onnxruntime_genai_asr":
        if artifact is None:
            raise FileNotFoundError("onnx model dir not found")
        return OnnxRuntimeGenaiAsr(artifact, language=candidate.get("language") or "en-US")
    raise ValueError(f"unsupported ASR backend: {backend}")
