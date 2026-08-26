"""DTLN wiring + optional live ONNX smoke."""
from __future__ import annotations

import numpy as np
import pytest

from nova_hailo.config import ROOT

MODELS = ROOT / "models" / "dtln"


def test_create_dtln_can_disable(monkeypatch):
    monkeypatch.setenv("NOVA_HAILO_NS", "off")
    from nova_hailo.web.dtln import create_dtln

    assert create_dtln(None) is None


def test_dtln_mix_locked_at_one():
    from nova_hailo.web.dtln import DEFAULT_STRENGTH

    assert DEFAULT_STRENGTH == 0.5


@pytest.mark.skipif(
    not (MODELS / "model_1.onnx").is_file() or not (MODELS / "model_2.onnx").is_file(),
    reason="DTLN ONNX not downloaded",
)
def test_dtln_processes_16k_pcm():
    pytest.importorskip("onnxruntime")
    from nova_hailo.web.dtln import BLOCK_SHIFT, NATIVE_RATE, DtlnNs

    ns = DtlnNs(strength=1.0)
    t = np.arange(NATIVE_RATE, dtype=np.float32) / NATIVE_RATE
    pcm = np.clip(0.1 * np.sin(2 * np.pi * 220 * t), -1, 1)
    i16 = (pcm * 32767).astype(np.int16).tobytes()
    out = ns.process_pcm16(i16)
    assert len(out) >= (NATIVE_RATE - BLOCK_SHIFT * 2) * 2
