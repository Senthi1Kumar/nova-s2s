"""Piper voice listing, and voice/speed changes that land at the next turn."""
from __future__ import annotations

import threading

import numpy as np
import pytest

from nova_hailo.backends import tts as tts_mod
from nova_hailo.backends.tts import PIPER_SPEEDS, PiperTTS, list_piper_voices


def _touch_voice(d, stem, with_json=True):
    (d / f"{stem}.onnx").write_bytes(b"")
    if with_json:
        (d / f"{stem}.onnx.json").write_text("{}")


class _FakeVoice:
    def __init__(self, name, calls, gate=None):
        self.name = name
        self.calls = calls
        self.gate = gate

    def synthesize(self, text, syn_config=None):
        if self.gate is not None:
            self.gate.wait(timeout=5)
        self.calls.append((self.name, getattr(syn_config, "length_scale", None)))
        yield np.ones(8, dtype=np.float32)


@pytest.fixture
def piper(tmp_path, monkeypatch):
    monkeypatch.setattr("nova_hailo.backends.tts_cache.DEFAULT_CACHE_DIR", tmp_path / "cache")
    for stem in ("en_US-amy-low", "en_GB-alba-medium"):
        _touch_voice(tmp_path, stem)
    calls: list = []
    monkeypatch.setattr(
        "piper.PiperVoice.load",
        lambda path, *a, **k: _FakeVoice(tts_mod.Path(path).stem, calls),
    )
    monkeypatch.setattr(PiperTTS, "_start_worker", lambda self: None)
    tts = PiperTTS(model_path=str(tmp_path / "en_US-amy-low.onnx"), local_play=False)
    tts._calls = calls
    return tts


def test_lists_installed_voices(tmp_path):
    _touch_voice(tmp_path, "en_US-amy-low")
    _touch_voice(tmp_path, "en_GB-alba-medium")
    _touch_voice(tmp_path, "en_US-hfc_female-medium")
    _touch_voice(tmp_path, "orphan", with_json=False)
    voices = list_piper_voices(tmp_path)
    assert [v["id"] for v in voices] == [
        "en_GB-alba-medium",
        "en_US-amy-low",
        "en_US-hfc_female-medium",
    ]
    assert [v["label"] for v in voices] == ["Alba · UK", "Amy · US", "HFC female · US"]


def test_speed_maps_to_length_scale(piper):
    piper.set_speed("faster")
    piper.begin_turn()
    list(piper.synthesize_stream("hello"))
    assert piper._calls[-1][1] == PIPER_SPEEDS["faster"] == 0.71


def test_swap_waits_for_next_turn(piper):
    piper.set_voice("en_GB-alba-medium")
    assert piper.voice_id == "en_US-amy-low"
    list(piper.synthesize_stream("hello"))
    assert piper._calls[-1][0] == "en_US-amy-low"
    piper.begin_turn()
    assert piper.voice_id == "en_GB-alba-medium"
    list(piper.synthesize_stream("hello"))
    assert piper._calls[-1][0] == "en_GB-alba-medium"


def test_cache_keyed_by_voice_and_speed(piper):
    piper.set_speed("slow")
    piper.begin_turn()
    assert piper._pcm_cache.voice == "en_US-amy-low@slow"


def test_normal_speed_keeps_bare_cache_key(piper):
    piper.set_voice("en_GB-alba-medium")
    piper.begin_turn()
    assert piper._pcm_cache.voice == "en_GB-alba-medium"


def test_last_pending_voice_wins(piper):
    piper.set_voice("en_GB-alba-medium")
    piper.set_voice("en_US-amy-low")
    piper.begin_turn()
    assert piper.voice_id == "en_US-amy-low"


def test_unknown_values_refused(piper):
    with pytest.raises(ValueError, match="unknown_voice"):
        piper.set_voice("nope")
    with pytest.raises(ValueError, match="unknown_speed"):
        piper.set_speed("turbo")
    piper.begin_turn()
    assert (piper.voice_id, piper.speed) == ("en_US-amy-low", "normal")


def test_voice_choice_flags():
    assert PiperTTS.supports_voice_choice is True
    assert tts_mod.QueuedOnnxTTS.supports_voice_choice is False


def test_superseded_warm_never_stores_new_voice_under_old_key(tmp_path, monkeypatch):
    from nova_hailo.backends.tts_cache import PcmCache

    cache_dir = tmp_path / "cache"
    monkeypatch.setattr("nova_hailo.backends.tts_cache.DEFAULT_CACHE_DIR", cache_dir)
    for stem in ("en_US-amy-low", "en_GB-alba-medium"):
        _touch_voice(tmp_path, stem)
    calls: list = []
    gate = threading.Event()

    def load(path, *a, **k):
        name = tts_mod.Path(path).stem
        return _FakeVoice(name, calls, gate if name == "en_US-amy-low" else None)

    monkeypatch.setattr("piper.PiperVoice.load", load)
    monkeypatch.setattr("nova_hailo.edge_harness.tool_ack.all_ack_lines", lambda: ["one", "two"])
    monkeypatch.setattr(PiperTTS, "_prewarm_playback", lambda self: None)
    monkeypatch.setattr(PiperTTS, "_measure", lambda self, voice_id, voice: 0.1)
    monkeypatch.setattr(PiperTTS, "_start_worker", lambda self: self._warm_pcm_cache())
    warms: list = []
    real_warm = PcmCache.warm_async

    def spy(self, t, texts):
        th = real_warm(self, t, texts)
        warms.append(th)
        return th

    monkeypatch.setattr(PcmCache, "warm_async", spy)
    tts = PiperTTS(model_path=str(tmp_path / "en_US-amy-low.onnx"), local_play=False)
    amy_cache = tts._pcm_cache
    # amy's warm is blocked inside synthesize; swap before it finishes
    tts.set_voice("en_GB-alba-medium")
    tts.set_speed("slow")
    tts.begin_turn()
    gate.set()
    for t in warms:
        t.join(timeout=5)

    assert amy_cache.voice == "en_US-amy-low"
    assert tts._pcm_cache.voice == "en_GB-alba-medium@slow"
    assert [c for c in calls if c[0] == "en_US-amy-low"]
    assert all(c[1] == 1.0 for c in calls if c[0] == "en_US-amy-low")
    assert all(c[1] == 1.25 for c in calls if c[0] == "en_GB-alba-medium")


# ---- a voice must run well ahead of real time on this device ----

class _TimedVoice(_FakeVoice):
    """One second of audio that takes ``cost`` seconds of the fake clock to make."""

    def __init__(self, name, calls, clock, cost):
        super().__init__(name, calls)
        self.clock, self.cost = clock, cost

    def synthesize(self, text, syn_config=None):
        self.clock[0] += self.cost * getattr(syn_config, "length_scale", 1.0)
        self.calls.append((self.name, getattr(syn_config, "length_scale", None)))
        yield np.ones(22050, dtype=np.float32)


@pytest.fixture
def timed(tmp_path, monkeypatch):
    """Real-time factors measured on the Pi 5: amy-low 0.09, the medium voices 0.12, ryan-high 0.79."""
    monkeypatch.setattr("nova_hailo.backends.tts_cache.DEFAULT_CACHE_DIR", tmp_path / "cache")
    costs = {"en_US-amy-low": 0.09, "en_GB-alba-medium": 0.12, "en_US-ryan-high": 0.79,
             "en_US-lessac-medium": 0.4}
    for stem in costs:
        _touch_voice(tmp_path, stem)
    clock, calls = [0.0], []
    monkeypatch.setattr(tts_mod, "_clock", lambda: clock[0])
    monkeypatch.setattr("piper.PiperVoice.load", lambda path, *a, **k: _TimedVoice(
        tts_mod.Path(path).stem, calls, clock, costs[tts_mod.Path(path).stem]))
    monkeypatch.setattr(PiperTTS, "_start_worker", lambda self: None)
    return PiperTTS(model_path=str(tmp_path / "en_US-amy-low.onnx"), local_play=False)


def test_a_voice_too_slow_for_this_device_is_refused(timed):
    with pytest.raises(ValueError, match="voice_too_slow"):
        timed.set_voice("en_US-ryan-high")
    timed.begin_turn()
    assert timed.voice_id == "en_US-amy-low"


def test_a_medium_voice_keeps_up_even_at_the_fastest_speed(timed):
    timed.set_speed("faster")
    timed.set_voice("en_GB-alba-medium")
    timed.begin_turn()
    assert (timed.voice_id, timed.speed) == ("en_GB-alba-medium", "faster")


def test_a_speed_that_would_outrun_the_voice_is_refused(timed):
    timed.set_voice("en_US-lessac-medium")                 # 0.4 of real time at normal speed
    timed.begin_turn()
    timed.set_speed("fast")                                 # 0.4 / 0.83 = 0.48: still keeps up
    with pytest.raises(ValueError, match="voice_too_slow"):
        timed.set_speed("faster")                           # 0.4 / 0.71 = 0.56: it would not
    timed.begin_turn()
    assert timed.speed == "fast"
    with pytest.raises(ValueError, match="voice_too_slow"):  # both at once, judged together
        timed.set_voice("en_US-lessac-medium", speed="faster")
