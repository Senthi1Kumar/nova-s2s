from __future__ import annotations

import importlib.util

from nova_hailo.config import ROOT


def load_inventory_module():
    path = ROOT / "scripts" / "model_inventory.py"
    spec = importlib.util.spec_from_file_location("model_inventory", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_load_manifest():
    inv = load_inventory_module()
    manifest = inv.load_manifest(ROOT / "bench" / "model_matrix.v0.0.1.yaml")
    assert manifest["version"] == "0.0.1"
    assert manifest["target_runtime"]["hailort"] == "5.1.1"


def test_unsupported_gemma():
    inv = load_inventory_module()
    manifest = inv.load_manifest()
    gemma = [c for c in manifest["candidates"] if c["id"] == "llm_gemma4_e2b"][0]
    status = inv.candidate_status(gemma, None)
    assert status.support == "unsupported"
    assert "No official 5.1.1 HEF" in status.reason


def test_whisper_tiny_unsupported():
    inv = load_inventory_module()
    manifest = inv.load_manifest()
    tiny = [c for c in manifest["candidates"] if c["id"] == "asr_whisper_tiny_hailo"][0]
    status = inv.candidate_status(tiny, None)
    assert status.support == "unsupported"
    assert status.artifact_path is None
    assert "No official Whisper-Tiny HEF" in status.reason
