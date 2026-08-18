"""STT/VAD config acceptance (offline, no Hailo / no sidecar process).

Covers: v002 freeze config, gate defaults, wait-budget boundaries, endpointing
source guard. Complements tests/test_audio_gate.py and tests/test_nemo_wait_budget.py.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml

from nova_hailo.audio_gate import (
    DEFAULT_MIN_PEAK,
    DEFAULT_MIN_RMS,
    DEFAULT_MIN_SEC,
    DEFAULT_MIN_SPEECH_FRAC,
    accept_utterance,
)
from nova_hailo.web.nemo_wait_budget import (
    DEFAULT_NEMO_WAIT_BASE_S,
    DEFAULT_NEMO_WAIT_CAP_S,
    DEFAULT_NEMO_WAIT_FLOOR_S,
    DEFAULT_NEMO_WAIT_SCALE,
    nemo_wait_budget_s,
)

ROOT = Path(__file__).resolve().parents[1]
V002 = ROOT / "config.oem_v002_test.yaml"
SIDECAR = ROOT / "nova_hailo" / "backends" / "nemo_speech_sidecar.py"
FREEZE_DOC = ROOT / "docs" / "STACK.md"

EN_NEMO_MODEL = "models/nemo_speech/nemotron-speech-streaming-en-0.6b.q8_0.gguf"


def _load_v002() -> dict:
    data = yaml.safe_load(V002.read_text())
    assert isinstance(data, dict)
    return data


def test_v002_stt_freeze_is_en_nemo():
    cfg = _load_v002()
    model = cfg["model"]
    assert model["stt_engine"] == "nemo_speech"
    assert model["nemo_speech_model"] == EN_NEMO_MODEL
    assert int(model.get("nemo_rnnt_right_context", 1)) == 1
    # Rollback keys retained
    assert model.get("parakeet_model")
    assert model.get("parakeet_decoder")


def test_v002_gate_and_wait_knobs_match_defaults():
    voice = _load_v002()["voice"]
    assert float(voice["gate_min_rms"]) == DEFAULT_MIN_RMS
    assert float(voice["gate_min_peak"]) == DEFAULT_MIN_PEAK
    assert float(voice["gate_min_sec"]) == DEFAULT_MIN_SEC
    assert float(voice["gate_min_speech_frac"]) == DEFAULT_MIN_SPEECH_FRAC
    assert float(voice["nemo_wait_floor_s"]) == DEFAULT_NEMO_WAIT_FLOOR_S
    assert float(voice["nemo_wait_cap_s"]) == DEFAULT_NEMO_WAIT_CAP_S
    assert float(voice["nemo_wait_base_s"]) == DEFAULT_NEMO_WAIT_BASE_S
    assert float(voice["nemo_wait_scale"]) == DEFAULT_NEMO_WAIT_SCALE


def test_gate_defaults_reject_quiet_accept_speech():
    quiet = np.random.randn(16000).astype(np.float32) * 0.008
    ok_q, reason_q, st_q = accept_utterance(quiet)
    assert not ok_q
    assert "low_rms" in reason_q or "low_speech" in reason_q or "low_peak" in reason_q
    assert st_q["rms"] < DEFAULT_MIN_RMS

    t = np.arange(16000, dtype=np.float32) / 16000.0
    env = (np.sin(2 * np.pi * 3 * t) * 0.5 + 0.5).astype(np.float32)
    speech = (np.sin(2 * np.pi * 180 * t) * env * 0.35).astype(np.float32)
    ok_s, reason_s, st_s = accept_utterance(speech)
    assert ok_s, (reason_s, st_s)


def test_nemo_wait_budget_boundaries():
    # floor
    assert nemo_wait_budget_s(0.0) == DEFAULT_NEMO_WAIT_FLOOR_S
    assert nemo_wait_budget_s(0.2) == DEFAULT_NEMO_WAIT_FLOOR_S
    # linear mid
    assert abs(nemo_wait_budget_s(1.0) - 0.65) < 1e-9
    # cap
    assert nemo_wait_budget_s(10.0) == DEFAULT_NEMO_WAIT_CAP_S
    assert nemo_wait_budget_s(-1.0) == DEFAULT_NEMO_WAIT_FLOOR_S


def test_sidecar_endpointing_hard_off():
    body = SIDECAR.read_text()
    assert "--asr.endpointing.enable=false" in body
    assert "endpointing.enable=true" not in body


def test_stack_doc_present():
    assert FREEZE_DOC.is_file()
    text = FREEZE_DOC.read_text()
    assert "nemo_speech" in text
    assert EN_NEMO_MODEL in text or "nemotron-speech-streaming-en-0.6b" in text
    assert "endpointing" in text.lower()
    assert "parakeet" in text.lower()
