"""LLM settings for displays: GET/POST /v1/settings logic and the nova.settings frame."""
import asyncio
import sys
import threading
import types
from unittest.mock import MagicMock

try:  # the Pi has the Hailo SDK; the laptop gets a stub so the pipeline imports
    import hailo_platform  # noqa: F401
except ImportError:
    _hp = types.ModuleType("hailo_platform")
    _hp.VDevice = MagicMock()
    _genai = types.ModuleType("hailo_platform.genai")
    _genai.LLM = _genai.Speech2Text = _genai.Speech2TextTask = MagicMock()
    _hp.genai = _genai
    sys.modules["hailo_platform"], sys.modules["hailo_platform.genai"] = _hp, _genai

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
    import nova_hailo.pipeline as pl

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
    import nova_hailo.pipeline as pl

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
