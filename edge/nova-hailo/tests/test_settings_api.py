"""LLM settings for displays: GET/POST /v1/settings logic and the nova.settings frame."""
import asyncio
import logging
import sys
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

try:  # the Pi has the Hailo SDK; the laptop gets a stub so the pipeline imports
    import hailo_platform  # noqa: F401
except ImportError:
    _hp = types.ModuleType("hailo_platform")
    _hp.VDevice = MagicMock()
    _genai = types.ModuleType("hailo_platform.genai")
    _genai.LLM = _genai.Speech2Text = _genai.Speech2TextTask = MagicMock()
    _hp.genai = _genai
    sys.modules["hailo_platform"], sys.modules["hailo_platform.genai"] = _hp, _genai

import nova_hailo.pipeline as pl  # noqa: E402
import nova_hailo.web.app as app_mod  # noqa: E402
from nova_hailo.backends.failover_llm import FailoverLLM  # noqa: E402
from nova_hailo.pipeline import NovaPipeline  # noqa: E402
from nova_hailo.settings_store import load_settings, save_settings  # noqa: E402
from nova_hailo.web import settings_ops as so  # noqa: E402
from nova_hailo.web.observe import ObserverHub  # noqa: E402


class Cloud:
    def __init__(self, provider, model):
        self.provider, self.model = provider, model

    def release(self):
        pass


class Pipe:
    def __init__(self, llm, backend):
        self.llm, self.llm_backend = llm, backend
        self._local_hef_alias = "qwen2"
        self.vdevice = object()
        self.applied = []

    def apply_llm_settings(self, **kw):
        self.applied.append(kw)
        if kw.get("provider") == "cerebras":
            raise RuntimeError("CEREBRAS_API_KEY is not set")
        self.llm_backend = kw.get("provider") or "python"
        return {}


def test_settings_name_the_provider_model_and_choices():
    pipe = Pipe(FailoverLLM(Cloud("openrouter", "openai/gpt-oss-120b"),
                            Cloud("groq", "openai/gpt-oss-120b")), "openrouter")
    s = so.llm_settings(pipe, env={"OPENROUTER_API_KEY": "k", "GROQ_API_KEY": "g"})
    assert s["provider"] == "openrouter" and s["model"] == "openai/gpt-oss-120b"
    assert s["failover"] is True and s["failover_ready"] is True
    by_id = {p["id"]: p for p in s["providers"]}
    assert by_id["openrouter"]["ok"] and by_id["groq"]["ok"] and not by_id["cerebras"]["ok"]
    assert "openai/gpt-oss-120b" in by_id["openrouter"]["models"]
    assert "qwen2" in by_id["local"]["models"]


def test_settings_for_the_on_device_model():
    s = so.llm_settings(Pipe(object(), "python"), env={})
    assert s["provider"] == "local" and s["model"] == "qwen2"
    assert s["failover"] is True and s["failover_ready"] is False   # switch on, nothing to fail over


def test_a_choice_is_applied_and_answered_with_the_new_settings():
    pipe = Pipe(Cloud("openrouter", "x"), "openrouter")
    ok, out = asyncio.run(so.apply_llm_choice(pipe, {"llm": {"provider": "groq",
                                                              "model": "openai/gpt-oss-20b"}}))
    assert ok and pipe.applied[-1] == {"mode": "openrouter", "provider": "groq",
                                       "or_model": "openai/gpt-oss-20b"}
    assert out["provider"] == "groq"
    ok, out = asyncio.run(so.apply_llm_choice(pipe, {"llm": {"provider": "local"}}))
    assert ok and pipe.applied[-1] == {"mode": "local", "local_hef": None}


def test_bad_choices_are_refused_with_a_reason():
    pipe = Pipe(Cloud("openrouter", "x"), "openrouter")
    assert asyncio.run(so.apply_llm_choice(pipe, {})) == (False, "missing_llm")
    assert asyncio.run(so.apply_llm_choice(pipe, {"llm": {"provider": "acme"}})) == \
        (False, "unknown_provider")
    ok, err = asyncio.run(so.apply_llm_choice(pipe, {"llm": {"provider": "cerebras"}}))
    assert not ok and "CEREBRAS_API_KEY" in err


def test_the_pipeline_switches_cloud_provider_at_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(pl, "save_settings", lambda data: save_settings(data, tmp_path / "s.json"))
    monkeypatch.setenv("GROQ_API_KEY", "g")
    p = NovaPipeline.__new__(NovaPipeline)
    p._genai_lock = threading.Lock()
    p.cloud_provider, p.llm_backend = "openrouter", "openrouter"
    p.llm = Cloud("openrouter", "openai/gpt-oss-120b")
    p._or_model, p._local_hef_alias = "openai/gpt-oss-120b", "qwen2"
    p.max_history_turns = p.cloud_history_turns = 2
    p.history = type("H", (), {"max_turns": 5})()
    made = []
    p._make_llm = lambda backend, hef, model: made.append((backend, model)) or Cloud(backend, model)
    p._evict_llm = lambda: None
    p.llm_settings_snapshot = lambda: {}
    p.apply_llm_settings(mode="openrouter", provider="groq")
    assert p.cloud_provider == "groq" and p.llm_backend == "groq"
    assert made == [("groq", "openai/gpt-oss-120b")]
    assert load_settings(tmp_path / "s.json")["cloud_provider"] == "groq"


def test_the_saved_provider_survives_a_restart(tmp_path):
    path = tmp_path / "s.json"
    assert load_settings(path)["cloud_provider"] == ""        # unset: config decides
    save_settings({"cloud_provider": "groq"}, path)
    assert load_settings(path)["cloud_provider"] == "groq"


def test_observers_get_the_latest_settings_when_they_join():
    hub = ObserverHub()
    hub.publish_session_event({"type": "nova.settings", "llm": {"provider": "groq"}})
    assert {"type": "nova.settings", "llm": {"provider": "groq"}} in hub.replay()


def test_the_failover_switch_is_applied_saved_and_reported():
    pipe = Pipe(Cloud("openrouter", "x"), "openrouter")
    ok, out = asyncio.run(so.apply_llm_choice(pipe, {"llm": {"provider": "openrouter",
                                                              "failover": False}}))
    assert ok and pipe.applied[-1]["failover"] is False
    pipe._failover_enabled = False
    assert so.llm_settings(pipe, env={})["failover"] is False
    ok, _ = asyncio.run(so.apply_llm_choice(pipe, {"llm": {"provider": "groq", "failover": "yes"}}))
    assert "failover" not in pipe.applied[-1]           # only a real bool is taken


def test_the_pipeline_rebuilds_its_llm_when_failover_is_switched(monkeypatch, tmp_path):
    monkeypatch.setattr(pl, "save_settings", lambda data: save_settings(data, tmp_path / "s.json"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    p = NovaPipeline.__new__(NovaPipeline)
    p._genai_lock = threading.Lock()
    p.cloud_provider, p.llm_backend = "openrouter", "openrouter"
    p.llm = Cloud("openrouter", "openai/gpt-oss-120b")
    p._or_model, p._local_hef_alias = "openai/gpt-oss-120b", "qwen2"
    p.max_history_turns = p.cloud_history_turns = 2
    p.history = type("H", (), {"max_turns": 5})()
    p._failover_enabled = True
    made = []
    p._make_llm = lambda backend, hef, model: made.append(p._failover_enabled) or Cloud(backend, model)
    p._evict_llm = lambda: None
    p.llm_settings_snapshot = lambda: {}
    p.apply_llm_settings(mode="openrouter", failover=False)   # same provider and model
    assert made == [False] and p._failover_enabled is False
    assert load_settings(tmp_path / "s.json")["failover"] is False


def test_the_failover_switch_survives_a_restart(tmp_path):
    path = tmp_path / "s.json"
    assert load_settings(path)["failover"] is None          # unset: config decides
    save_settings({"failover": False}, path)
    assert load_settings(path)["failover"] is False



# -- tts + avatar ---------------------------------------------------------

VOICES = ("en_US-amy-low", "en_GB-alba-medium")


class FakePiper:
    supports_voice_choice = True
    enabled = True

    def __init__(self, voices_dir, voice="en_US-amy-low"):
        self.model_path = str(voices_dir / f"{voice}.onnx")
        self.voice_id, self.speed = voice, "normal"
        self.pending_voice = self.pending_speed = None

    def set_voice(self, voice_id, models_dir=None, speed=None):
        if voice_id not in VOICES:
            raise ValueError("unknown_voice")
        self.pending_voice = voice_id

    def set_speed(self, name):
        if name not in ("slow", "normal", "fast"):
            raise ValueError("unknown_speed")
        self.pending_speed = name

    is_speaking = False

    def preview(self, text, voice_id, speed):
        self.previews = getattr(self, "previews", []) + [(text, voice_id, speed)]

    def begin_turn(self):
        self.voice_id = self.pending_voice or self.voice_id
        self.speed = self.pending_speed or self.speed
        return 1


class FakeKokoro:
    supports_voice_choice = False
    enabled = True


class Recorder:
    def __init__(self):
        self.events = []

    def publish_session_event(self, ev):
        self.events.append(ev)


class Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "hmi_settings.json"
    monkeypatch.setattr("nova_hailo.settings_store.SETTINGS_PATH", path)
    monkeypatch.setattr(pl, "save_settings", lambda data: save_settings(data, path))
    monkeypatch.setattr("nova_hailo.backends.tts_cache.DEFAULT_CACHE_DIR", tmp_path / "cache")
    return path


def make_pipe(tmp_path, tts=None, saved=None):
    for stem in VOICES:
        (tmp_path / f"{stem}.onnx").write_bytes(b"")
        (tmp_path / f"{stem}.onnx.json").write_text("{}")
    p = NovaPipeline.__new__(NovaPipeline)
    p.llm, p.llm_backend = Cloud("openrouter", "x"), "openrouter"
    p._local_hef_alias, p.vdevice = "qwen2", object()
    p.tts = tts if tts is not None else FakePiper(tmp_path)
    p._hmi_settings = load_settings()
    p._hmi_settings.update(saved or {})
    p.avatar = p._hmi_settings["avatar"]
    return p


@pytest.fixture
def served(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(app_mod, "OBSERVERS", rec)
    yield rec
    monkeypatch.setattr(app_mod, "_pipeline", None)


def post(pipe, body):
    app_mod._pipeline = pipe
    return asyncio.run(app_mod.settings_set(Req(body)))


def test_get_includes_tts_and_avatar(tmp_path, store, served):
    app_mod._pipeline = make_pipe(tmp_path)
    out = asyncio.run(app_mod.settings_get())
    assert set(out) == {"llm", "tts", "avatar"}
    assert [v["id"] for v in out["tts"]["voices"]] == sorted(VOICES)
    assert out["tts"]["voice"] == "en_US-amy-low" and out["tts"]["speed"] == "normal"
    assert out["avatar"] == "voice"


def test_startup_frame_carries_llm_tts_and_avatar(tmp_path, store):
    ev = app_mod.startup_settings_event(make_pipe(tmp_path, saved={"avatar": "gem"}))
    assert ev["type"] == "nova.settings" and ev["avatar"] == "gem"
    assert ev["llm"]["provider"] and ev["tts"]["voice"] == "en_US-amy-low"


def test_post_tts_and_avatar_persist(tmp_path, store, served):
    pipe = make_pipe(tmp_path)
    out = post(pipe, {"tts": {"voice": "en_GB-alba-medium", "speed": "fast"}, "avatar": "gem"})
    assert out["ok"] is True and out["errors"] == {}
    saved = load_settings(store)
    assert (saved["tts_voice"], saved["tts_speed"], saved["avatar"]) == \
        ("en_GB-alba-medium", "fast", "gem")
    pipe.tts.begin_turn()
    assert out["settings"]["avatar"] == "gem"
    assert pipe.tts.voice_id == "en_GB-alba-medium" and pipe.tts.speed == "fast"
    assert len(served.events) == 1
    ev = served.events[0]
    assert ev["type"] == "nova.settings" and ev["avatar"] == "gem"
    assert ev["tts"]["speed"] == "fast" and "llm" in ev


def test_partial_refusal_keeps_good_parts(tmp_path, store, served):
    pipe = make_pipe(tmp_path)
    out = post(pipe, {"tts": {"speed": "turbo"}, "avatar": "cloud"})
    assert out["ok"] is False and out["error"] == "unknown_speed"
    assert out["errors"] == {"tts": "unknown_speed"}
    assert out["settings"]["avatar"] == "cloud"
    assert load_settings(store)["avatar"] == "cloud"
    assert load_settings(store)["tts_speed"] == "normal"
    assert len(served.events) == 1
    assert post(pipe, {"tts": "x"})["errors"] == {"tts": "bad_tts"}
    assert post(pipe, {"avatar": "robot"})["errors"] == {"avatar": "unknown_avatar"}
    assert post(pipe, {"tts": {"voice": "nope"}})["errors"] == {"tts": "unknown_voice"}
    assert post(pipe, {})["error"] == "missing_llm"
    assert len(served.events) == 1                      # refusals publish nothing


def test_a_refused_voice_does_not_apply_the_speed(tmp_path, store, served):
    pipe = make_pipe(tmp_path)
    out = post(pipe, {"tts": {"voice": "nope", "speed": "fast"}})
    assert out["errors"] == {"tts": "unknown_voice"}
    assert pipe.tts.pending_speed is None
    assert load_settings(store)["tts_speed"] == "normal"


def test_non_piper_reports_no_voices(tmp_path, store, served):
    pipe = make_pipe(tmp_path, tts=FakeKokoro())
    assert so.tts_settings(pipe) == {"voice": "", "speed": "normal", "voices": []}
    out = post(pipe, {"tts": {"speed": "fast"}})
    assert out["errors"] == {"tts": "no_voice_choice"}


def test_llm_part_still_works_beside_the_others(tmp_path, store, served):
    pipe = make_pipe(tmp_path)
    pipe.apply_llm_settings = lambda **kw: {}
    out = post(pipe, {"llm": {"provider": "acme"}, "avatar": "drop"})
    assert out["errors"] == {"llm": "unknown_provider"} and out["settings"]["avatar"] == "drop"


def test_missing_persisted_voice_falls_back(tmp_path, store, served, caplog):
    pipe = make_pipe(tmp_path, saved={"tts_voice": "en_US-ryan-high"})
    with caplog.at_level(logging.WARNING):
        pipe._apply_saved_tts()
    assert "en_US-ryan-high" in caplog.text
    pipe.tts.begin_turn()
    assert pipe.tts.voice_id == "en_US-amy-low"
    app_mod._pipeline = pipe
    assert "tts" in asyncio.run(app_mod.settings_get())


def test_startup_applies_persisted_voice_and_speed(tmp_path, store):
    pipe = make_pipe(tmp_path, saved={"tts_speed": "slow", "tts_voice": "en_GB-alba-medium"})
    pipe._apply_saved_tts()
    pipe.tts.begin_turn()
    assert pipe.tts.speed == "slow" and pipe.tts.voice_id == "en_GB-alba-medium"


def test_startup_leaves_a_non_piper_engine_alone(tmp_path, store):
    pipe = make_pipe(tmp_path, tts=FakeKokoro(), saved={"tts_speed": "slow"})
    pipe._apply_saved_tts()


class BrokenPiper(FakePiper):
    def set_voice(self, voice_id, models_dir=None, speed=None):
        raise RuntimeError("onnx: truncated model")


def test_a_voice_that_fails_to_load_is_refused_and_the_rest_applies(tmp_path, store, served):
    pipe = make_pipe(tmp_path, tts=BrokenPiper(tmp_path))
    out = post(pipe, {"tts": {"voice": "en_GB-alba-medium"}, "avatar": "gem"})
    assert out["ok"] is False and out["errors"] == {"tts": "voice_load_failed"}
    assert out["settings"]["avatar"] == "gem" and load_settings(store)["avatar"] == "gem"
    assert load_settings(store)["tts_voice"] == ""
    assert len(served.events) == 1 and served.events[0]["avatar"] == "gem"


def test_startup_survives_a_voice_that_fails_to_load(tmp_path, store, caplog):
    pipe = make_pipe(tmp_path, tts=BrokenPiper(tmp_path),
                     saved={"tts_voice": "en_GB-alba-medium", "tts_speed": "fast"})
    with caplog.at_level(logging.WARNING):
        pipe._apply_saved_tts()
    assert "en_GB-alba-medium" in caplog.text
    pipe.tts.begin_turn()
    assert pipe.tts.voice_id == "en_US-amy-low" and pipe.tts.speed == "fast"   # the speed still applies


# -- preview ---------------------------------------------------------------


def preview(pipe, body):
    app_mod._pipeline = pipe
    return asyncio.run(app_mod.settings_preview(Req(body)))


def test_preview_speaks_without_saving(tmp_path, store, served):
    pipe = make_pipe(tmp_path)
    out = preview(pipe, {"voice": "en_GB-alba-medium", "speed": "fast"})
    assert out == {"ok": True}
    assert pipe.tts.previews == [(so.PREVIEW_LINE, "en_GB-alba-medium", "fast")]
    assert pipe.tts.voice_id == "en_US-amy-low" and pipe.tts.speed == "normal"
    assert pipe.tts.pending_voice is None and pipe.tts.pending_speed is None
    assert not store.exists() and served.events == []


def test_preview_defaults_to_the_current_voice(tmp_path, store, served):
    pipe = make_pipe(tmp_path)
    assert preview(pipe, {"speed": "slow"}) == {"ok": True}
    assert pipe.tts.previews == [(so.PREVIEW_LINE, "en_US-amy-low", "slow")]


def test_preview_refused_while_speaking(tmp_path, store, served):
    pipe = make_pipe(tmp_path)
    pipe.tts.is_speaking = True
    assert preview(pipe, {"voice": "en_GB-alba-medium"}) == {"ok": False, "error": "speaking"}
    assert not hasattr(pipe.tts, "previews")


def test_preview_refusals(tmp_path, store, served):
    pipe = make_pipe(tmp_path)
    assert preview(pipe, {"voice": "nope"})["error"] == "unknown_voice"
    assert preview(pipe, {"speed": "turbo"})["error"] == "unknown_speed"
    assert preview(make_pipe(tmp_path, tts=FakeKokoro()), {})["error"] == "no_voice_choice"
    app_mod._pipeline = None
    assert asyncio.run(app_mod.settings_preview(Req({}))) == {"ok": False, "error": "starting"}
    assert not hasattr(pipe.tts, "previews")


def test_preview_survives_a_voice_that_fails_to_load(tmp_path, store, served):
    class Broken(FakePiper):
        def preview(self, text, voice_id, speed):
            raise RuntimeError("onnx: truncated model")

    pipe = make_pipe(tmp_path, tts=Broken(tmp_path))
    assert preview(pipe, {"voice": "en_GB-alba-medium"}) == \
        {"ok": False, "error": "voice_load_failed"}


def test_preview_says_not_ready_when_tts_is_off(tmp_path, store, served):
    class Off(FakePiper):
        ready = False

        def preview(self, text, voice_id, speed):
            return False

    class Busy(Off):
        ready = True

    assert preview(make_pipe(tmp_path, tts=Off(tmp_path)), {}) == {"ok": False, "error": "not_ready"}
    assert preview(make_pipe(tmp_path, tts=Busy(tmp_path)), {}) == {"ok": False, "error": "speaking"}


class Voice:
    def __init__(self, name):
        self.name = name


def bare_piper(tmp_path, load=None):
    import queue

    from nova_hailo.backends.tts import PiperTTS

    t = PiperTTS.__new__(PiperTTS)
    t._settings_lock = threading.Lock()
    t._voice, t._voice_id, t._speed = Voice("amy"), "en_US-amy-low", "normal"
    t._pending_voice = t._pending_speed = None
    t.enabled, t._ready, t._pcm_cache = True, True, None
    t.model_path = str(tmp_path / "en_US-amy-low.onnx")
    t._gen_id = t._active_gen = t._synth_gen = 0
    t._gen_lock = threading.Lock()
    t._pending_lock = threading.Lock()
    t._pending = 0
    t._speaking = False
    t._idle = threading.Event()
    t._idle.set()
    t._interrupt = threading.Event()
    t._q = queue.Queue()
    t._preview = None
    t._snapshot = None
    t.first_audio_time = None
    t.total_synth_ms = t.total_play_ms = 0.0
    t._warm_pcm_cache = lambda voice=None: None
    t._stream_with = lambda voice, scale, text: iter([((voice.name, scale, text), 22050)])
    t._load_voice = load or (lambda path: Voice(Path(path).stem))
    return t


def test_piper_preview_leaves_the_active_voice_alone(tmp_path):
    from nova_hailo.backends.tts import PIPER_SPEEDS

    t = bare_piper(tmp_path)
    t.first_audio_time, t.total_synth_ms = 5.0, 9.0
    assert t.preview("hello", "en_GB-alba-medium", "fast") is True
    gid, text, _, _ = t._q.get_nowait()
    assert text == "hello" and gid == t._active_gen
    t._synth_gen = gid
    assert list(t.synthesize_stream(text)) == \
        [(("en_GB-alba-medium", PIPER_SPEEDS["fast"], "hello"), 22050)]
    assert t.voice_id == "en_US-amy-low" and t.speed == "normal" and t._pending_voice is None
    assert (t.first_audio_time, t.total_synth_ms) == (5.0, 9.0)
    t._synth_gen = gid + 1                   # a later item: the active voice again
    assert list(t.synthesize_stream("x"))[0][0][:2] == ("amy", 1.0)


class Handle:
    def __init__(self, state):
        self.state = state

    def control_state(self):
        return self.state

    def control_mute(self, muted):
        pass


@pytest.fixture
def session_state():
    from nova_hailo.web.session_control import CONTROL

    handle = Handle("THINKING")
    unregister = CONTROL.register(handle)
    yield handle
    unregister()


def test_preview_refused_while_a_turn_is_starting(tmp_path, store, served, session_state):
    t = bare_piper(tmp_path)
    pipe = make_pipe(tmp_path, tts=t)
    gid = t.begin_turn()                     # the turn has begun; nothing is queued yet
    assert t.is_speaking is False
    assert preview(pipe, {"voice": "en_GB-alba-medium"}) == {"ok": False, "error": "speaking"}
    assert t._active_gen == gid and t._q.empty() and t._preview is None
    session_state.state = "LISTENING"
    assert preview(pipe, {"voice": "en_GB-alba-medium"}) == {"ok": True}


def test_reply_starting_during_preview_load_wins(tmp_path, store, served):
    loading, release = threading.Event(), threading.Event()

    def slow_load(path):
        loading.set()
        release.wait(5)
        return Voice(Path(path).stem)

    t = bare_piper(tmp_path, load=slow_load)
    pipe = make_pipe(tmp_path, tts=t)
    app_mod._pipeline = pipe
    result = {}
    th = threading.Thread(target=lambda: result.update(
        asyncio.run(app_mod.settings_preview(Req({"voice": "en_GB-alba-medium"})))))
    th.start()
    assert loading.wait(5)
    gid = t.begin_turn()                     # the reply starts while the voice loads
    release.set()
    th.join(5)
    assert result == {"ok": False, "error": "speaking"}
    assert t._active_gen == gid and t._q.empty() and t._preview is None


def test_preview_voice_released(tmp_path):
    t = bare_piper(tmp_path)
    t.preview("hello", "en_GB-alba-medium", "fast")
    gid = t._active_gen
    t._release_preview(gid)                  # the worker, once the item has played
    assert t._preview is None
    t.preview("hello", "en_GB-alba-medium", "fast")
    t._speaking, t._idle = False, threading.Event()
    t.begin_turn()                           # or the next turn, before it plays
    assert t._preview is None
    t._release_preview(t._active_gen)


def test_protocol_copies_identical():
    import filecmp
    from pathlib import Path as P

    root = P(__file__).resolve().parents[1]
    assert filecmp.cmp(root / "docs/api/realtime.md",
                       root / "skills/nova-realtime-client/references/protocol.md",
                       shallow=False)
