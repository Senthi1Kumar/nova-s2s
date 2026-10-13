"""LiveKit agent worker wiring, proven offline: no SFU, no models, no audio.

EchoAgent.session is a read-only property that resolves through the agent's
activity (livekit/agents/voice/agent.py), so it cannot be assigned. The tests
install a fake activity via _get_activity_or_raise instead, which is the exact
seam the property reads, so the real property code runs.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

HAS_LIVEKIT = (
    importlib.util.find_spec("livekit.agents") is not None
    and importlib.util.find_spec("livekit.plugins.silero") is not None
)
pytestmark = pytest.mark.skipif(not HAS_LIVEKIT, reason="livekit extra not installed")


class _FakeSession:
    def __init__(self) -> None:
        self.said: list[str] = []

    def say(self, text, **_kwargs):
        self.said.append(text)
        return SimpleNamespace()


def _agent_with_session():
    from nova_hailo.lk import agent as agent_mod

    echo = agent_mod.EchoAgent()
    session = _FakeSession()
    echo._get_activity_or_raise = lambda: SimpleNamespace(session=session)  # type: ignore[method-assign]
    return echo, session


def _turn(text):
    return SimpleNamespace(text_content=text)


def _run(coro):
    return asyncio.run(coro)


def test_agent_module_imports():
    from nova_hailo.lk import agent as agent_mod

    assert callable(agent_mod.entrypoint)
    assert agent_mod.server is not None


def test_echo_says_what_it_heard_then_stops_response():
    from livekit.agents import StopResponse

    echo, session = _agent_with_session()
    with pytest.raises(StopResponse):
        _run(echo.on_user_turn_completed(None, _turn("  turn on the lights ")))
    assert session.said == ["You said: turn on the lights"]


@pytest.mark.parametrize("text", ["", "   \n", None])
def test_echo_empty_turn_says_didnt_catch_then_stops_response(text):
    from livekit.agents import StopResponse

    echo, session = _agent_with_session()
    with pytest.raises(StopResponse):
        _run(echo.on_user_turn_completed(None, _turn(text)))
    assert session.said == ["I didn't catch that."]


def test_build_stt_fails_clearly_when_artifacts_missing(monkeypatch):
    from nova_hailo.lk import agent as agent_mod

    monkeypatch.setattr(agent_mod, "resolve_parakeet_paths", lambda model, lib: (None, None))
    cfg = SimpleNamespace(get=lambda *keys, default=None: default)
    with pytest.raises(RuntimeError, match="parakeet model/lib not found"):
        agent_mod._build_stt(cfg)


def test_build_stt_fails_when_only_one_artifact_found(monkeypatch):
    from nova_hailo.lk import agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "resolve_parakeet_paths", lambda model, lib: (Path("/m.gguf"), None)
    )
    cfg = SimpleNamespace(get=lambda *keys, default=None: default)
    with pytest.raises(RuntimeError, match="parakeet model/lib not found"):
        agent_mod._build_stt(cfg)


def _capture_create_tts(monkeypatch):
    from nova_hailo.lk import agent as agent_mod

    seen: dict = {}

    def fake_create_tts(engine, **kwargs):
        seen["engine"] = engine
        seen.update(kwargs)
        return "engine-sentinel"

    monkeypatch.setattr(agent_mod, "create_tts", fake_create_tts)
    return agent_mod, seen


def test_build_tts_makes_relative_piper_path_absolute(monkeypatch):
    agent_mod, seen = _capture_create_tts(monkeypatch)
    cfg = SimpleNamespace(get=lambda *keys, default=None: "models/piper/en_US-amy-low.onnx")
    assert agent_mod._build_tts(cfg) == "engine-sentinel"
    assert seen["engine"] == "piper"
    assert seen["piper_path"] == str(agent_mod.ROOT / "models/piper/en_US-amy-low.onnx")
    assert Path(seen["piper_path"]).is_absolute()


def test_build_tts_keeps_absolute_path_and_none(monkeypatch):
    agent_mod, seen = _capture_create_tts(monkeypatch)
    agent_mod._build_tts(SimpleNamespace(get=lambda *k, default=None: "/abs/voice.onnx"))
    assert seen["piper_path"] == "/abs/voice.onnx"
    agent_mod._build_tts(SimpleNamespace(get=lambda *k, default=None: None))
    assert seen["piper_path"] is None


# --- F1: models load once per process in the prewarm hook, not in the entrypoint

def _stub_builders(monkeypatch, agent_mod, calls):
    """Replace the three blocking loaders with recorders returning sentinels."""
    monkeypatch.setattr(agent_mod, "_build_stt", lambda cfg: calls.append("stt") or "stt-engine")
    monkeypatch.setattr(agent_mod, "_build_tts", lambda cfg: calls.append("tts") or "tts-engine")
    monkeypatch.setattr(agent_mod.silero.VAD, "load", staticmethod(lambda: calls.append("vad") or "vad"))
    monkeypatch.setattr(agent_mod, "NovaSTT", lambda engine: ("NovaSTT", engine))
    monkeypatch.setattr(agent_mod, "NovaTTS", lambda engine: ("NovaTTS", engine))
    monkeypatch.setattr(agent_mod.AppConfig, "load", staticmethod(lambda: SimpleNamespace()))


def test_server_registers_prewarm_as_its_setup_hook():
    """AgentServer.setup_fnc is the per-process initialiser (worker.py:417)."""
    from nova_hailo.lk import agent as agent_mod

    assert agent_mod.server.setup_fnc is agent_mod.prewarm


def test_prewarm_builds_every_model_into_proc_userdata(monkeypatch):
    from nova_hailo.lk import agent as agent_mod

    calls: list[str] = []
    _stub_builders(monkeypatch, agent_mod, calls)
    proc = SimpleNamespace(userdata={})
    agent_mod.prewarm(proc)
    assert sorted(calls) == ["stt", "tts", "vad"]
    assert proc.userdata["stt"] == ("NovaSTT", "stt-engine")
    assert proc.userdata["tts"] == ("NovaTTS", "tts-engine")
    assert proc.userdata["vad"] == "vad"


def test_entrypoint_uses_prebuilt_models_and_loads_nothing(monkeypatch):
    from nova_hailo.lk import agent as agent_mod

    calls: list[str] = []
    _stub_builders(monkeypatch, agent_mod, calls)
    seen: dict = {}

    class _Session:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def on(self, event, callback):
            seen.setdefault("events", []).append(event)

        async def start(self, **kwargs):
            seen["start"] = kwargs

    monkeypatch.setattr(agent_mod, "AgentSession", _Session)

    async def _connect():
        return None

    prebuilt = {"stt": "S", "tts": "T", "vad": "V"}
    ctx = SimpleNamespace(
        proc=SimpleNamespace(userdata=prebuilt),
        connect=_connect,
        room=SimpleNamespace(name="room-1"),
    )
    _run(agent_mod.entrypoint(ctx))
    assert calls == [], f"entrypoint loaded models on the event loop: {calls}"
    assert (seen["stt"], seen["tts"], seen["vad"]) == ("S", "T", "V")
    assert "llm" not in seen, "llm must stay omitted, not passed as None"
    assert isinstance(seen["start"]["agent"], agent_mod.EchoAgent)


# --- F2: at most one job and one warm idle process on an 8 GB Pi

def test_server_keeps_at_most_one_idle_process(monkeypatch):
    """num_idle_processes (worker.py:332) defaults to the CPU count in prod."""
    from nova_hailo.lk import agent as agent_mod

    assert agent_mod.server._num_idle_processes == 1


def test_server_allows_a_slow_prewarm():
    """initialize_process_timeout (worker.py:335) defaults to 10 s and kills the
    process; Parakeet + Piper + Silero on a Pi need far longer."""
    from nova_hailo.lk import agent as agent_mod

    assert agent_mod.server._initialize_process_timeout >= 60


def test_single_job_load_reports_full_only_while_a_job_runs():
    from nova_hailo.lk import agent as agent_mod

    assert agent_mod.single_job_load(SimpleNamespace(active_jobs=[])) == 0.0
    assert agent_mod.single_job_load(SimpleNamespace(active_jobs=[object()])) == 1.0
    assert agent_mod.server.load_fnc is agent_mod.single_job_load


@pytest.mark.parametrize("devmode", [False, True])
def test_worker_is_available_for_one_job_and_full_for_a_second(monkeypatch, devmode):
    """Drives the SDK's own availability logic (worker.py:1372-1397) with our config."""
    from nova_hailo.lk import agent as agent_mod

    server = agent_mod.server
    for name, value in {
        "_devmode": devmode,
        "_draining": False,
        "_simulation": False,
        "_worker_load": 0.0,
        "_reserved_slots": 0,
    }.items():
        monkeypatch.setattr(server, name, value, raising=False)
    monkeypatch.setattr(type(server), "active_jobs", property(lambda self: []))

    assert server._is_available() is True, "an idle worker must accept a job"

    monkeypatch.setattr(server, "_reserved_slots", 1)  # accepted, not yet launched
    assert server._is_available() is False, "a concurrent second request must be refused"

    monkeypatch.setattr(server, "_reserved_slots", 0)
    monkeypatch.setattr(server, "_worker_load", agent_mod.single_job_load(SimpleNamespace(active_jobs=[1])))
    monkeypatch.setattr(type(server), "active_jobs", property(lambda self: [object()]))
    assert server._is_available() is False, "a running job must fill the worker"
