"""LiveKit extra wiring: the extra is declared, and the API we subclass exists.

Callers: pytest. Skips the import half cleanly when the extra is not installed,
so the suite still passes on a machine that never ran `uv sync --extra livekit`.
No date schemas.

The base-class test is not ceremony. nova_hailo/lk/tts_plugin.py and
stt_plugin.py subclass these classes and override private methods (`_run`,
`_recognize_impl`) that are not a stable public API. Pinning them here means a
version bump fails with one obvious assertion rather than a confusing error
inside a running agent.
"""
from __future__ import annotations

import importlib.util
import tomllib

import pytest

from nova_hailo.config import ROOT

HAS_LIVEKIT = importlib.util.find_spec("livekit.agents") is not None
requires_livekit = pytest.mark.skipif(
    not HAS_LIVEKIT, reason="livekit extra not installed"
)


def test_extra_is_declared_and_pinned():
    with open(ROOT / "pyproject.toml", "rb") as fh:
        extras = tomllib.load(fh)["project"]["optional-dependencies"]

    assert "livekit" in extras, "the livekit extra must be declared"
    assert any(d.startswith("livekit-agents==") for d in extras["livekit"]), (
        "pin livekit-agents exactly: we subclass its plugin base classes and "
        "override private methods, which are not a stable API"
    )


def test_the_extra_does_not_disturb_the_audio_or_http_pins():
    """The demo path keeps the versions it was validated against.

    sounddevice owns the HMI's microphone; fastapi/starlette/uvicorn are pinned
    together by project convention. onnxruntime is deliberately absent here --
    it is unpinned and does move when the extra installs.
    """
    with open(ROOT / "pyproject.toml", "rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]

    pinned = {d.split("==")[0]: d for d in deps if "==" in d}
    assert pinned.get("sounddevice") == "sounddevice==0.5.1"
    assert pinned.get("fastapi") == "fastapi==0.115.6"
    assert pinned.get("starlette") == "starlette==0.41.3"


@requires_livekit
def test_the_base_classes_this_integration_subclasses_exist():
    from livekit.agents import stt as lk_stt
    from livekit.agents import tts as lk_tts

    assert hasattr(lk_tts.TTS, "synthesize")
    assert hasattr(lk_tts.ChunkedStream, "_run")
    assert hasattr(lk_tts, "AudioEmitter")
    assert hasattr(lk_tts, "TTSCapabilities")
    assert hasattr(lk_stt.STT, "_recognize_impl")
    assert hasattr(lk_stt, "STTCapabilities")
    assert lk_stt.SpeechEventType.FINAL_TRANSCRIPT


@requires_livekit
def test_agent_session_still_excludes_none_from_the_llm_type():
    """agent.py omits `llm` rather than passing None, on the strength of this.

    Neither form raises at construction, so the annotation is the only thing
    saying which is correct. If a future version admits None, this fails and
    the explanatory comment in agent.py can go.
    """
    import inspect

    from livekit.agents import AgentSession

    params = inspect.signature(AgentSession.__init__).parameters
    llm_ann = str(params["llm"].annotation)
    vad_ann = str(params["vad"].annotation)

    assert "None" not in llm_ann, f"llm now admits None: {llm_ann}"
    assert "None" in vad_ann, f"vad no longer admits None: {vad_ann}"
