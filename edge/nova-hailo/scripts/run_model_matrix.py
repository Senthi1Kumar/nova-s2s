#!/usr/bin/env python3
"""Run the Nova-Hailo v0.0.1 model matrix harness."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
from model_inventory import DEFAULT_MANIFEST, find_declared_artifact, inventory, load_manifest

from nova_hailo.bench.contracts import (
    UNMEASURED,
    UNSUPPORTED,
    CaseResult,
    ComponentSummary,
    append_jsonl,
    cer,
    host_fingerprint,
    response_contract_ok,
    sample_resources,
    summarize_latency,
    wer,
    write_json,
)
from nova_hailo.config import ROOT

CORPUS = ROOT / "bench" / "corpus"
DEFAULT_LOG_ROOT = ROOT / "logs" / "model-matrix"
COMPONENTS = {"wake", "vad", "asr", "llm", "tts"}
MODES = {"inventory", "wake", "vad", "asr", "llm", "tts", "all", "report"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def now_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def hailo_available() -> bool:
    return importlib.util.find_spec("hailo_platform") is not None


def case_slice(rows: list[dict[str, Any]], protocol: dict[str, Any], max_cases: int | None) -> list[dict[str, Any]]:
    minimum = int(protocol.get("min_measured_cases") or len(rows))
    cap = min(len(rows), minimum)
    if max_cases is not None:
        cap = min(cap, max(0, int(max_cases)))
    return rows[:cap]


def unmeasured_case(candidate: dict[str, Any], case_id: str, reason: str, *, ok: bool = True) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        candidate_id=str(candidate["id"]),
        component=str(candidate.get("component") or "unknown"),
        ok=ok,
        metrics={"support_reason": candidate.get("reason"), "unmeasured_reason": reason, "ts": time.time()},
        resources=sample_resources().to_dict(),
        measured=False,
    )


def synthetic_latency(rng: random.Random, base: float, spread: float) -> float:
    return round(base + rng.random() * spread, 2)


def run_wake_candidate(candidate: dict[str, Any], cases: list[dict[str, Any]]) -> list[CaseResult]:
    """PTT baseline is always measured; openWakeWord stays experimental until engine installed."""
    backend = str(candidate.get("backend") or "none")
    if candidate.get("id") == "wake_ptt_baseline" or backend in {"none", "ptt"}:
        return [
            CaseResult(
                case_id=row.get("id", f"wake_{i}"),
                candidate_id=str(candidate["id"]),
                component="wake",
                ok=True,
                metrics={
                    "ts": time.time(),
                    "mode": "push_to_talk",
                    "detection_latency_ms": 0.0,
                    "false_accept": False,
                },
                resources=sample_resources().to_dict(),
                measured=True,
            )
            for i, row in enumerate(cases, start=1)
        ]
    return [
        unmeasured_case(candidate, row.get("id", f"wake_{i}"), "openWakeWord engine not installed in desk harness")
        for i, row in enumerate(cases, start=1)
    ]



def _pcm16_tone(duration_ms: int, sr: int = 16000, speech: bool = True) -> np.ndarray:
    n = max(1, int(sr * duration_ms / 1000.0))
    t = np.arange(n, dtype=np.float32) / sr
    if speech:
        # Multi-harmonic burst approximating voiced energy for VAD/ASR smoke.
        x = 0.35 * np.sin(2 * np.pi * 180 * t)
        x += 0.20 * np.sin(2 * np.pi * 360 * t)
        x += 0.08 * np.sin(2 * np.pi * 720 * t)
        env = np.clip(t * 8.0, 0, 1) * np.clip((t[-1] - t) * 8.0, 0, 1)
        x *= env
    else:
        x = 0.01 * np.random.default_rng(0).standard_normal(n).astype(np.float32)
    return np.clip(x * 32767.0, -32768, 32767).astype(np.int16)


def _asr_case_audio(row: dict[str, Any]) -> tuple[np.ndarray, int, str]:
    """Real recording when the case has one, else Piper resynthesis.

    Recorded audio is the only honest basis for a WER claim: TTS resynthesis
    measures the pipeline, not microphone/far-field quality. Returns
    (audio, sample_rate, source) so every row records which basis it used.
    """
    # NOVA_ASR_DISTANCE=near|far selects a condition when both were recorded, so
    # each run scores every engine on one consistent acoustic condition.
    want = (os.environ.get("NOVA_ASR_DISTANCE") or "").strip().lower()
    paths = row.get("audio_paths") or {}
    rel = str((paths.get(want) if want else None) or row.get("audio_path") or "").strip()
    if rel:
        path = (CORPUS / rel) if not Path(rel).is_absolute() else Path(rel)
        if path.is_file():
            import soundfile as sf

            audio, sr = sf.read(str(path), dtype="float32")
            audio = np.asarray(audio, dtype=np.float32)
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            return audio.reshape(-1), int(sr), "recorded"
    audio, sr = _float_audio_from_tts(row["reference"], prefer="piper")
    return audio, sr, "tts_resynth"


_TTS_CACHE: dict[str, Any] = {}


def _float_audio_from_tts(text: str, prefer: str = "piper") -> tuple[np.ndarray, int]:
    """Synthesize reference audio for ASR when corpus wavs are absent.

    The engine is cached per process. Constructing one per utterance leaked an
    ONNX session and a worker thread every call — `release()` does not exist on
    these classes, so the AttributeError was silently swallowed — which OOM'd the
    Pi after ~80 calls (measured 2026-07-30). Reuse also removes ~2.2s of model
    load per utterance.
    """
    tts = _TTS_CACHE.get(prefer)
    if tts is None:
        from nova_hailo.backends.tts import create_tts

        tts = create_tts(engine=prefer, enabled=True, local_play=False, wait_for_play=False)
        _TTS_CACHE[prefer] = tts
    if not getattr(tts, "enabled", False):
        # Fallback tone
        pcm = _pcm16_tone(1500, speech=True)
        return pcm.astype(np.float32) / 32768.0, 16000
    audio, sr = tts.synthesize(text)
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    return audio, int(sr)


def _resample_mono(audio: np.ndarray, src_sr: int, dst_sr: int = 16000) -> np.ndarray:
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    if src_sr == dst_sr or len(x) == 0:
        return x
    duration = len(x) / float(src_sr)
    n = max(1, int(duration * dst_sr))
    xp = np.linspace(0.0, 1.0, num=len(x), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n, endpoint=False)
    return np.interp(x_new, xp, x).astype(np.float32)


def run_vad_candidate(candidate: dict[str, Any], cases: list[dict[str, Any]], rng: random.Random) -> list[CaseResult]:
    import time as _time


    backend = str(candidate.get("backend") or "")
    artifact = find_declared_artifact(candidate)
    rows: list[CaseResult] = []

    if backend == "silero_onnx":
        if artifact is None or importlib.util.find_spec("onnxruntime") is None:
            reason = (
                "silero artifact present; onnxruntime not installed on this host"
                if artifact is not None
                else "silero artifact missing"
            )
            return [unmeasured_case(candidate, row["id"], reason) for row in cases]
        from nova_hailo.web.silero_vad import SileroVadConfig, SileroVadSegmenter

        seg = SileroVadSegmenter(SileroVadConfig(onnx_path=str(artifact)))
        for row in cases:
            kind = row["kind"]
            expected = kind == "speech"
            pcm = _pcm16_tone(int(row["duration_ms"]), speech=expected)
            t0 = _time.perf_counter()
            events = seg.feed(pcm.tobytes())
            # flush with a bit of silence to force endpoint
            events += seg.feed(_pcm16_tone(400, speech=False).tobytes())
            dt = (_time.perf_counter() - t0) * 1000.0
            detected = any(str(e[0]) == "speech_started" for e in events)
            if kind == "speech":
                ok = bool(detected)
            elif kind == "silence":
                ok = not bool(detected)
            else:
                ok = True  # noise: informational only
            rows.append(
                CaseResult(
                    case_id=row["id"],
                    candidate_id=str(candidate["id"]),
                    component="vad",
                    ok=ok,
                    metrics={
                        "ts": time.time(),
                        "kind": kind,
                        "duration_ms": row["duration_ms"],
                        "detection_latency_ms": round(dt, 2) if expected else None,
                        "false_accept": bool(detected) and not expected,
                        "events": [str(e[0]) for e in events],
                        "api": "silero_onnx_real",
                    },
                    resources=sample_resources().to_dict(),
                    measured=True,
                )
            )
            seg.reset()
        return rows

    if backend == "webrtcvad":
        if importlib.util.find_spec("webrtcvad") is None:
            return [unmeasured_case(candidate, row["id"], "webrtcvad package not installed on this host") for row in cases]
        from nova_hailo.web.vad import VadConfig, WebRtcVadSegmenter

        seg = WebRtcVadSegmenter(VadConfig())
        for row in cases:
            kind = row["kind"]
            expected = kind == "speech"
            pcm = _pcm16_tone(int(row["duration_ms"]), speech=expected)
            t0 = _time.perf_counter()
            events = seg.feed(pcm.tobytes())
            events += seg.feed(_pcm16_tone(400, speech=False).tobytes())
            dt = (_time.perf_counter() - t0) * 1000.0
            detected = any(e[0] == "speech_started" for e in events)
            rows.append(
                CaseResult(
                    case_id=row["id"],
                    candidate_id=str(candidate["id"]),
                    component="vad",
                    ok=detected == expected if kind == "speech" else not detected,
                    metrics={
                        "ts": time.time(),
                        "kind": kind,
                        "duration_ms": row["duration_ms"],
                        "detection_latency_ms": round(dt, 2) if expected else None,
                        "false_accept": bool(detected and not expected),
                        "api": "webrtcvad_real",
                    },
                    resources=sample_resources().to_dict(),
                    measured=True,
                )
            )
            seg.reset()
        return rows

    return [unmeasured_case(candidate, row["id"], f"{backend or 'vad'} runtime/artifact unavailable") for row in cases]


_CPU_ASR_BACKENDS = {
    "parakeet_capi",
    "transcribe_cpp",
    "transformers_asr",
    "onnxruntime_genai_asr",
}


def _run_cpu_asr_candidate(
    candidate: dict[str, Any], cases: list[dict[str, Any]], artifact
) -> list[CaseResult]:
    """CPU ASR engines (parakeet / transcribe.cpp / transformers / ORT-GenAI)."""
    import time as _time

    backend = str(candidate.get("backend") or "")
    if artifact is None and backend not in {"transformers_asr"}:
        return [
            unmeasured_case(
                candidate,
                row["id"],
                "ASR artifact not found on host (download model first)",
            )
            for row in cases
        ]
    try:
        from nova_hailo.bench.asr_adapters import create_asr_engine
    except Exception as exc:  # noqa: BLE001
        return [unmeasured_case(candidate, row["id"], f"adapter import: {exc}") for row in cases]

    # Refuse rather than OOM: a 0.6B q8_0 needs >1GB and the Pi has 8GB shared
    # with the resident LLM/STT. Loading blind froze the box on 2026-07-30.
    need_mb = float(candidate.get("expected_memory_mb") or 0) + 500.0
    avail_mb = sample_resources().mem_available_mb
    if avail_mb is not None and need_mb and avail_mb < need_mb:
        return [
            unmeasured_case(
                candidate,
                row["id"],
                f"insufficient memory: {avail_mb:.0f}MB available, need ~{need_mb:.0f}MB "
                "(stop the demo server / recorder first)",
            )
            for row in cases
        ]

    cold_t0 = _time.perf_counter()
    try:
        engine = create_asr_engine(candidate, artifact)
    except Exception as exc:  # noqa: BLE001
        return [unmeasured_case(candidate, row["id"], f"engine load: {exc}") for row in cases]
    cold_ms = (_time.perf_counter() - cold_t0) * 1000.0

    rows: list[CaseResult] = []
    try:
        try:  # warmup, excluded from measured cases
            warm, wsr, _s = _asr_case_audio(cases[0])
            engine.transcribe(_resample_mono(warm, wsr, 16000))
        except Exception:
            pass
        for i, row in enumerate(cases):
            ref = row["reference"]
            audio, sr, src = _asr_case_audio(row)
            audio = _resample_mono(audio, sr, 16000)
            t0 = _time.perf_counter()
            try:
                hyp = engine.transcribe(audio) or ""
                err = None
            except Exception as exc:  # noqa: BLE001
                hyp, err = "", repr(exc)
            infer_ms = (_time.perf_counter() - t0) * 1000.0
            dur_s = len(audio) / 16000.0
            rows.append(
                CaseResult(
                    case_id=row["id"],
                    candidate_id=str(candidate["id"]),
                    component="asr",
                    ok=err is None and bool(hyp),
                    metrics={
                        "ts": time.time(),
                        "reference": ref,
                        "hypothesis": hyp,
                        "wer": round(wer(ref, hyp), 4),
                        "cer": round(cer(ref, hyp), 4),
                        "infer_ms": round(infer_ms, 2),
                        "rtf": round((infer_ms / 1000.0) / dur_s, 3) if dur_s > 0 else None,
                        "audio_sec": round(dur_s, 3),
                        "audio_source": src,
                        "cold_load_ms": round(cold_ms, 2) if i == 0 else None,
                        "engine": backend,
                        "decoder": candidate.get("decoder"),
                        "abi": getattr(engine, "abi_version", None),
                        "error": err,
                        "api": backend,
                        "model": str(artifact or candidate.get("hf_id") or ""),
                    },
                    resources=sample_resources().to_dict(),
                    measured=True,
                    cold=(i == 0),
                )
            )
    finally:
        try:
            engine.release()
        except Exception:
            pass
    return rows


def run_asr_candidate(candidate: dict[str, Any], cases: list[dict[str, Any]], hardware: bool) -> list[CaseResult]:
    import time as _time

    artifact = find_declared_artifact(candidate)
    support = str(candidate.get("support") or "")
    if support == UNSUPPORTED:
        return [unmeasured_case(candidate, row["id"], str(candidate.get("reason") or support)) for row in cases]
    if candidate.get("backend") == "cpu_whisper_ref":
        return [
            unmeasured_case(candidate, row["id"], "CPU Whisper reference not wired in v0.0.1 matrix; Hailo Whisper-Base is primary")
            for row in cases
        ]
    if candidate.get("backend") in _CPU_ASR_BACKENDS:
        return _run_cpu_asr_candidate(candidate, cases, artifact)
    can_measure = bool(hardware and hailo_available() and artifact)
    if not can_measure:
        return [
            unmeasured_case(
                candidate,
                row["id"],
                "ASR requires --hardware, hailo_platform, and declared HEF",
            )
            for row in cases
        ]

    from hailo_platform import VDevice

    from nova_hailo.backends.stt import WhisperSTT

    rows: list[CaseResult] = []
    vdevice = VDevice()
    stt = None
    try:
        cold_t0 = _time.perf_counter()
        stt = WhisperSTT(vdevice, str(artifact))
        cold_ms = (_time.perf_counter() - cold_t0) * 1000.0
        # warmup
        warm_audio, warm_sr = _float_audio_from_tts("Nova cabin check one two three.", prefer="piper")
        warm_audio = _resample_mono(warm_audio, warm_sr, 16000)
        try:
            stt.transcribe(warm_audio)
        except Exception:
            pass
        for i, row in enumerate(cases):
            ref = row["reference"]
            audio, sr, src = _asr_case_audio(row)
            audio = _resample_mono(audio, sr, 16000)
            t0 = _time.perf_counter()
            try:
                hyp = stt.transcribe(audio) or ""
                err = None
            except Exception as exc:
                hyp = ""
                err = repr(exc)
            infer_ms = (_time.perf_counter() - t0) * 1000.0
            w = wer(ref, hyp)
            c = cer(ref, hyp)
            rows.append(
                CaseResult(
                    case_id=row["id"],
                    candidate_id=str(candidate["id"]),
                    component="asr",
                    ok=err is None and bool(hyp),
                    metrics={
                        "ts": time.time(),
                        "reference": ref,
                        "hypothesis": hyp,
                        "wer": round(w, 4),
                        "cer": round(c, 4),
                        "infer_ms": round(infer_ms, 2),
                        "cold_load_ms": round(cold_ms, 2) if i == 0 else None,
                        "hallucination": (not hyp) and err is None,
                        "error": err,
                        "api": "hailo_speech2text_real",
                        "audio_source": src,
                        "hef": str(artifact),
                    },
                    resources=sample_resources().to_dict(),
                    measured=True,
                    cold=(i == 0),
                )
            )
    finally:
        if stt is not None:
            stt.release()
        try:
            vdevice.release()
        except Exception:
            pass
    return rows


def run_llm_candidate(candidate: dict[str, Any], cases: list[dict[str, Any]], hardware: bool) -> list[CaseResult]:
    import time as _time

    artifact = find_declared_artifact(candidate)
    support = str(candidate.get("support") or "")
    if support == UNSUPPORTED:
        return [unmeasured_case(candidate, row["id"], str(candidate.get("reason") or support)) for row in cases]
    can_measure = bool(hardware and hailo_available() and artifact)
    if not can_measure:
        return [
            unmeasured_case(candidate, row["id"], "LLM requires --hardware, hailo_platform, and declared HEF")
            for row in cases
        ]

    from hailo_platform import VDevice

    from nova_hailo.backends.llm import HailoLLM

    rows: list[CaseResult] = []
    vdevice = VDevice()
    llm = None
    try:
        cold_t0 = _time.perf_counter()
        llm = HailoLLM(
            vdevice,
            str(artifact),
            temperature=0.15,
            seed=42,
            max_tokens=48,
            no_think=True,
        )
        cold_ms = (_time.perf_counter() - cold_t0) * 1000.0
        # warmup
        try:
            llm.generate([{"role": "user", "content": "Say ready."}], max_tokens=8, quiet=True)
            llm.clear()
        except Exception:
            pass
        for i, row in enumerate(cases):
            messages = [
                {
                    "role": "system",
                    "content": "You are Nova, an in-cabin voice assistant. Reply in one short spoken sentence. No tools. No special tokens.",
                },
                {"role": "user", "content": row["text"]},
            ]
            try:
                text, metrics = llm.generate(messages, max_tokens=48, quiet=True)
                err = None
            except Exception as exc:
                text, metrics, err = "", None, repr(exc)
            try:
                llm.clear()
            except Exception:
                pass
            mdict = metrics.to_dict() if metrics is not None else {}
            contract = response_contract_ok(text or "")
            rows.append(
                CaseResult(
                    case_id=row["id"],
                    candidate_id=str(candidate["id"]),
                    component="llm",
                    ok=err is None and bool(contract["ok"]),
                    metrics={
                        "ts": time.time(),
                        "prompt_category": row.get("category"),
                        "expect_tool": row.get("expect_tool"),
                        "response": text,
                        "ttft_ms": mdict.get("ttft_ms"),
                        "decode_tok_s": mdict.get("decode_tok_s"),
                        "tok_s": mdict.get("tok_s"),
                        "generated_tokens": mdict.get("generated_tokens"),
                        "llm_total_ms": mdict.get("llm_total_ms"),
                        "cold_load_ms": round(cold_ms, 2) if i == 0 else None,
                        "response_contract": contract,
                        "response_contract_ok": contract["ok"],
                        "error": err,
                        "api": "hailo_genai_llm_real",
                        "hef": str(artifact),
                    },
                    resources=sample_resources().to_dict(),
                    measured=True,
                    cold=(i == 0),
                )
            )
    finally:
        if llm is not None:
            try:
                llm.release()
            except Exception:
                pass
        try:
            vdevice.release()
        except Exception:
            pass
    return rows


def run_tts_candidate(candidate: dict[str, Any], cases: list[dict[str, Any]], rng: random.Random) -> list[CaseResult]:
    import time as _time

    backend = str(candidate.get("backend") or "")
    artifact = find_declared_artifact(candidate)
    if backend == "pocket_tts_pytorch":
        # Official PyTorch runtime intentionally out of the ORT matrix; the ORT
        # lane is the tts_pocket_onnx candidate (sherpa-onnx int8, non-commercial).
        return [unmeasured_case(candidate, row["id"], f"{backend} runtime out of ORT matrix scope") for row in cases]

    cold_t0 = _time.perf_counter()
    api_label = None
    if backend in {"supertonic_onnx", "pocket_sherpa_onnx"}:
        from nova_hailo.bench.tts_adapters import create_matrix_tts

        try:
            synth, release_fn, api_label = create_matrix_tts(backend, artifact)
        except Exception as exc:
            return [unmeasured_case(candidate, row["id"], str(exc)) for row in cases]
    elif backend in {"piper_onnx", "kokoro_onnx"}:
        if artifact is None:
            return [unmeasured_case(candidate, row["id"], f"{backend} artifact missing") for row in cases]

        from nova_hailo.backends.tts import create_tts

        engine = "piper" if backend == "piper_onnx" else "kokoro"
        tts = create_tts(engine=engine, enabled=True, local_play=False, wait_for_play=False)
        if not getattr(tts, "enabled", False):
            return [unmeasured_case(candidate, row["id"], f"{engine} failed to load") for row in cases]
        synth, release_fn, api_label = tts.synthesize, tts.release, f"{engine}_onnx_real"
    else:
        return [unmeasured_case(candidate, row["id"], f"{backend or 'tts'} artifact/runtime unavailable") for row in cases]
    cold_ms = (_time.perf_counter() - cold_t0) * 1000.0
    try:  # warmup (excluded from measured cases)
        synth("Warm up check.")
    except Exception:
        pass

    rows: list[CaseResult] = []
    try:
        for i, row in enumerate(cases):
            text = row["text"]
            t0 = _time.perf_counter()
            try:
                audio, sr = synth(text)
                err = None
            except Exception as exc:
                audio, sr, err = np.zeros(0, dtype=np.float32), 22050, repr(exc)
            first_pcm_ms = (_time.perf_counter() - t0) * 1000.0
            audio = np.asarray(audio, dtype=np.float32).reshape(-1)
            dur = float(len(audio) / max(sr, 1))
            rtf = (first_pcm_ms / 1000.0) / dur if dur > 0 else None
            rows.append(
                CaseResult(
                    case_id=row["id"],
                    candidate_id=str(candidate["id"]),
                    component="tts",
                    ok=err is None and len(audio) > 0,
                    metrics={
                        "ts": time.time(),
                        "first_pcm_ms": round(first_pcm_ms, 2),
                        "rtf": round(rtf, 3) if rtf is not None else None,
                        "completeness": bool(len(audio) > 0),
                        "text_chars": len(text),
                        "audio_sec": round(dur, 3),
                        "sample_rate": int(sr),
                        "cold_load_ms": round(cold_ms, 2) if i == 0 else None,
                        "error": err,
                        "api": api_label,
                    },
                    resources=sample_resources().to_dict(),
                    measured=True,
                )
            )
    finally:
        try:
            release_fn()
        except Exception:
            pass
    return rows


def summarize_component(candidate: dict[str, Any], rows: list[CaseResult]) -> ComponentSummary:
    measured_rows = [r for r in rows if r.measured]
    metrics = [r.metrics for r in measured_rows]
    latency_keys = ["detection_latency_ms", "infer_ms", "ttft_ms", "decode_tok_s", "first_pcm_ms"]
    latency = {
        key: summarize_latency([m[key] for m in metrics if m.get(key) is not None])
        for key in latency_keys
    }
    # Cold model load is a separate axis from warm per-case latency — never mix.
    cold_loads = [m["cold_load_ms"] for m in metrics if m.get("cold_load_ms") is not None]
    if cold_loads:
        latency["cold_load_ms"] = summarize_latency(cold_loads)
    # Aggregate per-case resource samples (previously dropped as {}).
    resource_summary: dict[str, Any] = {}
    for rkey in ("rss_mb", "mem_available_mb", "swap_used_mb", "cpu_percent", "cpu_temp_c", "power_w"):
        vals = [
            float(r.resources[rkey])
            for r in measured_rows
            if isinstance(r.resources, dict) and r.resources.get(rkey) is not None
        ]
        if vals:
            resource_summary[rkey] = {
                "p50": round(sorted(vals)[len(vals) // 2], 2),
                "max": round(max(vals), 2),
                "min": round(min(vals), 2),
                "n": len(vals),
            }
    quality: dict[str, Any] = {}
    if metrics:
        for key in ("wer", "cer"):
            vals = [float(m[key]) for m in metrics if m.get(key) is not None]
            if vals:
                quality[key] = summarize_latency(vals)
        if any("response_contract_ok" in m for m in metrics):
            vals = [bool(m.get("response_contract_ok")) for m in metrics]
            quality["response_contract_pass_rate"] = round(sum(vals) / len(vals), 4) if vals else None
        if any("false_accept" in m for m in metrics):
            vals = [bool(m.get("false_accept")) for m in metrics]
            quality["false_accept_rate"] = round(sum(vals) / len(vals), 4) if vals else None
        if any("completeness" in m for m in metrics):
            vals = [bool(m.get("completeness")) for m in metrics]
            quality["completeness_rate"] = round(sum(vals) / len(vals), 4) if vals else None
    return ComponentSummary(
        candidate_id=str(candidate["id"]),
        component=str(candidate.get("component") or "unknown"),
        support=str(candidate.get("support") or UNMEASURED),
        n_cases=len(rows),
        n_ok=sum(1 for r in rows if r.ok),
        n_fail=sum(1 for r in rows if not r.ok),
        latency=latency,
        quality=quality,
        resources=resource_summary,
        notes=list({str(r.metrics.get("unmeasured_reason")) for r in rows if r.metrics.get("unmeasured_reason")}),
        measured=bool(measured_rows),
    )


def load_cases(component: str, protocol: dict[str, Any], max_cases: int | None) -> list[dict[str, Any]]:
    if component == "wake":
        # Reuse VAD case ids as wake timing placeholders for the PTT baseline.
        return case_slice(read_jsonl(CORPUS / "vad_cases.jsonl"), protocol, max_cases)
    path_map = {
        "vad": CORPUS / "vad_cases.jsonl",
        "asr": CORPUS / "asr_transcripts.jsonl",
        "llm": CORPUS / "llm_prompts.jsonl",
        "tts": CORPUS / "tts_texts.jsonl",
    }
    return case_slice(read_jsonl(path_map[component]), protocol, max_cases)


def write_rows(path: Path, rows: Iterable[CaseResult]) -> None:
    append_jsonl(path, [row.to_dict() for row in rows])


def selected_components(mode: str, component: str) -> set[str]:
    selected = component if component != "all" else mode
    if selected == "all":
        return set(COMPONENTS)
    if selected in COMPONENTS:
        return {selected}
    if selected in {"inventory", "report"}:
        return set()
    raise ValueError(f"unknown component/mode {selected}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", default="all", choices=sorted(MODES))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--out-dir")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--component", default="all")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--hardware", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--ids", default="", help="comma-separated candidate ids to include (empty = all)")
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    protocol = manifest.get("benchmark_protocol") or {}
    seed = args.seed if args.seed is not None else int(protocol.get("seed") or 42)
    rng = random.Random(seed)
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_LOG_ROOT / now_run_id()
    out_dir.mkdir(parents=True, exist_ok=True)

    statuses = inventory(args.manifest)
    inventory_rows = [s.to_dict() for s in statuses]
    write_json(out_dir / "manifest.json", manifest)
    write_json(out_dir / "inventory.json", inventory_rows)

    if args.mode == "inventory":
        write_json(out_dir / "summary.json", {"run_id": out_dir.name, "inventory_only": True})
        print(out_dir)
        return 0
    if args.mode == "report":
        print(out_dir)
        return 0

    previous: dict[str, Any] = {}
    summary_path = out_dir / "summary.json"
    if summary_path.exists() and not args.force:
        previous = json.loads(summary_path.read_text())
    completed = {
        item.get("candidate_id")
        for item in previous.get("components", [])
        if item.get("candidate_id")
    }

    components = selected_components(args.mode, args.component)
    summaries: list[dict[str, Any]] = []
    errors_path = out_dir / "errors.jsonl"
    include_ids = {x.strip() for x in (args.ids or "").split(",") if x.strip()}
    for candidate in manifest.get("candidates", []):
        component = str(candidate.get("component") or "")
        if component not in components:
            continue
        candidate_id = str(candidate["id"])
        if include_ids and candidate_id not in include_ids:
            continue
        if candidate_id in completed and not args.force:
            continue
        cases = load_cases(component, protocol, args.max_cases)
        support = str(candidate.get("support") or UNMEASURED)
        if support == UNSUPPORTED:
            rows = [
                unmeasured_case(candidate, row["id"], str(candidate.get("reason") or support))
                for row in cases
            ]
        else:
            try:
                if component == "wake":
                    rows = run_wake_candidate(candidate, cases)
                elif component == "vad":
                    rows = run_vad_candidate(candidate, cases, rng)
                elif component == "asr":
                    rows = run_asr_candidate(candidate, cases, args.hardware and not args.dry_run)
                elif component == "llm":
                    rows = run_llm_candidate(candidate, cases, args.hardware and not args.dry_run)
                elif component == "tts":
                    rows = run_tts_candidate(candidate, cases, rng)
                else:
                    rows = []
            except Exception as exc:
                append_jsonl(
                    errors_path,
                    [
                        {
                            "ts": time.time(),
                            "candidate_id": candidate_id,
                            "component": component,
                            "error": repr(exc),
                        }
                    ],
                )
                rows = [unmeasured_case(candidate, row["id"], repr(exc), ok=False) for row in cases]
        write_rows(out_dir / f"{component}.jsonl", rows)
        summaries.append(summarize_component(candidate, rows).to_dict())

    # Merge with prior sequential invocations (vad→asr→llm→tts share one out-dir);
    # otherwise each stage overwrites the previous stage's component summaries.
    if summary_path.exists():
        try:
            prior = json.loads(summary_path.read_text())
            fresh_ids = {s["candidate_id"] for s in summaries}
            summaries = [
                s for s in prior.get("components", []) if s.get("candidate_id") not in fresh_ids
            ] + summaries
        except Exception:
            pass
    summary = {
        "run_id": out_dir.name,
        "created_ts": time.time(),
        "out_dir": str(out_dir),
        "dry_run": bool(args.dry_run),
        "hardware": bool(args.hardware),
        "seed": seed,
        "host": host_fingerprint(),
        "protocol": protocol,
        "inventory": inventory_rows,
        "components": summaries,
    }
    write_json(summary_path, summary)
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
