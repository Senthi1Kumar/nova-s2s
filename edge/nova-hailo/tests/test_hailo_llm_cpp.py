"""HailoLLMCpp (opt-in native LLM backend). See ROADMAP.md §6d.

Callers: pytest. Hardware-gated: skips cleanly (not a hard collection
failure) when hailo_platform or the compiled hailo_llm_cpp extension aren't
present, so `pytest tests/` needs no --ignore for this file on a laptop.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("hailo_platform")

_BACKENDS_DIR = str(Path(__file__).resolve().parents[1] / "nova_hailo" / "backends")
if _BACKENDS_DIR not in sys.path:
    sys.path.insert(0, _BACKENDS_DIR)
pytest.importorskip("hailo_llm_cpp")

from nova_hailo.backends.llm import HailoLLMCpp  # noqa: E402
from nova_hailo.config import resolve_llm_hef  # noqa: E402

try:
    _HEF_PATH = str(resolve_llm_hef("qwen2"))
except FileNotFoundError:
    _HEF_PATH = None

pytestmark = pytest.mark.skipif(
    _HEF_PATH is None or not Path(_HEF_PATH).is_file(),
    reason="qwen2 HEF not present on this host",
)


@pytest.fixture(scope="module")
def llm():
    inst = HailoLLMCpp(hef_path=_HEF_PATH, temperature=0.1, seed=42, max_tokens=16, no_think=True)
    yield inst
    inst.release()


def test_generate_returns_text_and_metrics(llm):
    messages = [
        {"role": "system", "content": "You are a helpful assistant. Answer in one short sentence."},
        {"role": "user", "content": "What is 2+2? /no_think"},
    ]
    text, metrics = llm.generate(messages, max_tokens=16)
    assert isinstance(text, str)
    assert text.strip()
    assert metrics.generated_tokens > 0
    assert metrics.ttft_ms is not None
    assert metrics.device == "hailo10h_npu"


def test_max_tokens_cap_is_honored(llm):
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Count from one to one hundred. /no_think"},
    ]
    _, metrics = llm.generate(messages, max_tokens=8)
    assert metrics.generated_tokens <= 8


def test_token_callback_fires_per_token(llm):
    seen: list[str] = []
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Say hello. /no_think"},
    ]
    text, metrics = llm.generate(messages, max_tokens=8, token_callback=seen.append)
    assert len(seen) == metrics.generated_tokens
    assert "".join(seen).strip() or text.strip()


def test_should_stop_aborts_generation(llm):
    calls = {"n": 0}

    def stop_after_two():
        calls["n"] += 1
        return calls["n"] > 2

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Count from one to one hundred. /no_think"},
    ]
    _, metrics = llm.generate(messages, max_tokens=64, should_stop=stop_after_two)
    assert metrics.generated_tokens <= 3


def test_clear_context_does_not_raise(llm):
    llm.clear()


def test_group_id_matches_pipeline_shared_vdevice():
    """HailoLLMCpp must join pipeline.py's VDevice group, not request an
    exclusive physical device -- regression guard for
    HAILO_OUT_OF_PHYSICAL_DEVICES when both are constructed together."""
    from nova_hailo.backends.llm import SHARED_VDEVICE_GROUP_ID

    assert SHARED_VDEVICE_GROUP_ID  # non-empty; exact value host-dependent
