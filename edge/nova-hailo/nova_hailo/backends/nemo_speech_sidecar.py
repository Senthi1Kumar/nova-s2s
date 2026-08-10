"""Launches and health-checks NeMo-Speech.cpp's own HTTP/WebSocket server
(``nemo-speech serve``) as a local sidecar process.

Selected with ``model.stt_engine: nemo_speech`` in the live realtime pipeline
(``nova_hailo/web/realtime_session.py``) instead of driving the C ABI's
streaming functions directly from Python threads -- that hit an unresolved
hang (the native recognizer opened on one thread, streamed from a fresh
thread per turn). The sidecar offloads all chunking/threading/endpointing to
NVIDIA's own tested C++ server process; realtime_session.py talks to it as a
WebSocket client over its OpenAI-realtime-shaped ``/v1/realtime`` protocol.

Requires the ``cpu-server`` build preset (``NEMO_SPEECH_BUILD_HTTP=ON``) --
the default ``cpu-asr`` preset used for the offline/benchmark ctypes path
does not include the HTTP server at all.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

DEFAULT_PORT = 8090
_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_base_url: str | None = None


def _find_serve_binary() -> Path | None:
    root = Path(__file__).resolve().parent.parent.parent
    cands = [Path(p) for p in (os.environ.get("NOVA_NEMO_SPEECH_SERVE_BIN"),) if p]
    cands += [
        root / "cloned" / "NeMo-Speech.cpp" / "build" / "cpu-server" / "bin" / "nemo-speech",
    ]
    for p in cands:
        if p.is_file():
            return p
    return None


def _is_ready(base_url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/ready", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_sidecar(
    asr_model: str,
    vad_model: str | None = None,
    port: int = DEFAULT_PORT,
    *,
    rnnt_right_context: int = 1,
) -> str | None:
    """Start the nemo-speech serve sidecar if not already running.

    Returns its base URL, or None if the server binary/models are unavailable
    (callers fall back to the offline ctypes path in that case).

    Endpointing is always forced off (HARDCODED safety). ``rnnt_right_context``
    defaults to 1 (low-latency cache-aware); callers may pass
    ``model.nemo_rnnt_right_context`` from config.
    """
    global _proc, _base_url
    with _lock:
        base_url = f"http://127.0.0.1:{port}"
        if _proc is not None and _proc.poll() is None and _is_ready(base_url):
            return _base_url
        bin_path = _find_serve_binary()
        if not bin_path:
            print("[nemo_speech_sidecar] serve binary not found (build the cpu-server preset)")
            return None
        # Boolean flags are presence-only in this CLI (e.g. --asr.endpointing.enable
        # means true); a trailing "true"/"false" value errors with "unexpected
        # argument" (measured live). Use --flag=false to negate one.
        #
        # Endpointing MUST stay off for our live path (HARDCODED below; do not
        # wire to config): Silero VAD in realtime_session already cuts turns.
        # Server-side EOU emits mid-stream `.completed`, our receiver closes the
        # WS, subsequent PCM gets "send failed: sent 1000", and the turn burns
        # the wait with empty text (measured live 2026-08-07). Finals come only
        # from input_audio_buffer.commit → stream->finish().
        #
        # rnnt_right_context: low-latency cache-aware preset is 1 (docs/
        # asr/configuration.md). Optional config: model.nemo_rnnt_right_context.
        rc = max(0, int(rnnt_right_context))
        cmd = [
            str(bin_path),
            "serve",
            "--asr-model",
            str(asr_model),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-ui",
            # HARDCODED safety: never enable server-side endpointing.
            "--asr.endpointing.enable=false",
            "--asr.streaming.rnnt_right_context",
            str(rc),
            "--threads",
            "2",
        ]
        if vad_model:
            # Mask silence mel frames before the encoder (quality at distance).
            # Do NOT enable vad_based endpointing — same mid-stream close bug.
            cmd += [
                "--asr.vad.model_path",
                str(vad_model),
                "--asr.vad.masker.mask_enable",
            ]
        print(f"[nemo_speech_sidecar] launching: {' '.join(cmd)}")
        log_path = Path("/tmp/nemo_speech_sidecar.log")
        log_file = open(log_path, "ab")
        _proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
        print(f"[nemo_speech_sidecar] output logged to {log_path}")
        for _ in range(100):  # up to ~20s for model load
            if _is_ready(base_url):
                _base_url = base_url
                print(f"[nemo_speech_sidecar] ready at {base_url}")
                return base_url
            if _proc.poll() is not None:
                print(f"[nemo_speech_sidecar] process exited early (code {_proc.returncode})")
                return None
            time.sleep(0.2)
        print("[nemo_speech_sidecar] did not become ready in time")
        return None


def get_sidecar_url() -> str | None:
    return _base_url


def shutdown_sidecar() -> None:
    global _proc, _base_url
    with _lock:
        if _proc is not None and _proc.poll() is None:
            _proc.terminate()
            try:
                _proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _proc.kill()
        _proc = None
        _base_url = None
