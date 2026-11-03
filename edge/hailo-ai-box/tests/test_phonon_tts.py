import io
import sys
import threading
import time
import types
import wave
from typing import ClassVar

import numpy as np
import pytest

from aibox.backends.tts import PhononTts
from aibox.config import load_settings


class FakeAudio:
    sample_rate = 24000

    def __init__(self, model):
        self.model = model

    def __enter__(self):
        self.model.active_count += 1
        self.model.max_active = max(self.model.max_active, self.model.active_count)
        return self

    def __iter__(self):
        try:
            for chunk in (np.array([-1.0, -0.5], dtype=np.float32), np.array([0.0, 0.5, 1.0], dtype=np.float32)):
                time.sleep(0.02)
                yield chunk
        finally:
            self.model.active_count -= 1

    def __exit__(self, *args):
        self.model.active = False


class FakeTTS:
    instances: ClassVar[list] = []

    def __init__(self, lang, conditions=None):
        self.lang = lang
        self.conditions = conditions
        self.active = False
        self.active_count = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.instances.append(self)

    def stream(self, text):
        with self.lock:
            return FakeAudio(self)


async def collect(svc):
    return [chunk async for chunk in svc.synthesize_stream("hello")]


def test_stream_trims_trailing_silence_but_preserves_internal_pause():
    class Audio:
        sample_rate = 24000

        def __enter__(self):
            return self

        def __iter__(self):
            loud = np.full(480, 0.5, dtype=np.float32)
            quiet = np.zeros(480 * 30, dtype=np.float32)
            yield np.concatenate((loud, quiet, loud))
            yield quiet

        def __exit__(self, *args):
            pass

    class Voice:
        def stream(self, text):
            return Audio()

    svc = PhononTts()
    svc._voice = Voice()
    chunks = __import__("asyncio").run(collect(svc))
    pcm = np.frombuffer(b"".join(chunk for chunk, _ in chunks), "<i2")
    loud = np.flatnonzero(pcm > 1000)
    assert loud[0] == 0
    assert loud[480] == 480 + 480 * 30
    assert len(pcm) == 480 + 480 * 30 + 480 + 480 * 5


def test_load_and_stream_native_pcm16(monkeypatch):
    FakeTTS.instances.clear()
    monkeypatch.setitem(sys.modules, "ptts", types.SimpleNamespace(TTS=FakeTTS))
    svc = PhononTts()
    assert svc.load()
    chunks = __import__("asyncio").run(collect(svc))
    assert [rate for _, rate in chunks] == [24000, 24000]
    pcm = np.frombuffer(b"".join(chunk for chunk, _ in chunks), "<i2")
    assert pcm.tolist() == [-32767, -16384, 0, 16384, 32767]
    assert FakeTTS.instances[0].lang == "en"
    assert FakeTTS.instances[0].conditions == {"duration_delta": -0.25}


def test_synthesize_collects_stream_as_wav(monkeypatch):
    monkeypatch.setitem(sys.modules, "ptts", types.SimpleNamespace(TTS=FakeTTS))
    svc = PhononTts()
    svc.load()
    wav = __import__("asyncio").run(svc.synthesize("hello"))
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24000)
        assert np.frombuffer(w.readframes(w.getnframes()), "<i2").size == 5


def test_empty_text_is_rejected(monkeypatch):
    monkeypatch.setitem(sys.modules, "ptts", types.SimpleNamespace(TTS=FakeTTS))
    svc = PhononTts()
    svc.load()
    with pytest.raises(ValueError, match="empty text"):
        __import__("asyncio").run(svc.synthesize(" "))


def test_generation_is_serialized(monkeypatch):
    monkeypatch.setitem(sys.modules, "ptts", types.SimpleNamespace(TTS=FakeTTS))
    svc = PhononTts()
    svc.load()
    model = svc._voice

    async def two():
        import asyncio
        await asyncio.gather(collect(svc), collect(svc))

    __import__("asyncio").run(two())
    assert model.max_active == 1


def test_tts_backend_settings():
    assert load_settings({}).tts_backend == "phonon"
    assert load_settings({"AIBOX_TTS_BACKEND": "piper"}).tts_backend == "piper"
    assert load_settings({}).phonon_duration_delta == -0.25
    assert load_settings({"AIBOX_PHONON_DURATION_DELTA": "-0.15"}).phonon_duration_delta == -0.15
    with pytest.raises(ValueError, match="AIBOX_TTS_BACKEND"):
        load_settings({"AIBOX_TTS_BACKEND": "unknown"})
