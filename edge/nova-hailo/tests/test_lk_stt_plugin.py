"""ParakeetSTT behind LiveKit's STT interface. No models, no audio device."""
from __future__ import annotations

import asyncio
import importlib.util

import numpy as np
import pytest

HAS_LIVEKIT = importlib.util.find_spec("livekit.agents") is not None
pytestmark = pytest.mark.skipif(not HAS_LIVEKIT, reason="livekit extra not installed")


class _FakeSTT:
    def __init__(self, text="hello there"):
        self.text, self.seen = text, None

    def transcribe(self, audio, language=None):
        self.seen = audio
        return self.text


def _frame(n=1600, value=1000, rate=16000, channels=1):
    from livekit import rtc

    return rtc.AudioFrame(
        data=np.full(n * channels, value, dtype=np.int16).tobytes(),
        sample_rate=rate,
        num_channels=channels,
        samples_per_channel=n,
    )


def _tone_frame(freq, rate, seconds=0.5, amp=8000):
    from livekit import rtc

    t = np.arange(int(rate * seconds)) / rate
    pcm = (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16)
    return rtc.AudioFrame(pcm.tobytes(), rate, 1, pcm.size)


def _recognize(engine, buffer):
    from nova_hailo.lk.stt_plugin import NovaSTT

    async def _go():
        return await NovaSTT(engine).recognize(buffer)

    return asyncio.run(_go())


def test_transcript_comes_back_as_a_final_speech_event():
    from livekit.agents import stt as lk_stt

    ev = _recognize(_FakeSTT("hello there"), _frame())
    assert ev.type == lk_stt.SpeechEventType.FINAL_TRANSCRIPT
    assert ev.alternatives[0].text == "hello there"


def test_engine_receives_float32_mono_audio():
    engine = _FakeSTT()
    _recognize(engine, _frame(n=1600, value=32767))
    assert engine.seen is not None
    assert engine.seen.dtype == np.float32
    assert engine.seen.ndim == 1
    assert abs(float(engine.seen[0]) - 1.0) < 0.001


def test_zero_length_frame_returns_empty_transcript_not_an_exception():
    """A short or silent buffer is normal input, not a fault."""
    from livekit.agents import stt as lk_stt

    engine = _FakeSTT("")
    ev = _recognize(engine, _frame(n=0))
    assert ev.type == lk_stt.SpeechEventType.FINAL_TRANSCRIPT
    assert ev.alternatives[0].text == ""
    assert engine.seen is None  # nothing to transcribe, engine not called


def test_empty_frame_list_returns_empty_transcript_not_an_exception():
    engine = _FakeSTT("")
    ev = _recognize(engine, [])
    assert ev.alternatives[0].text == ""
    assert engine.seen is None


def test_a_list_of_frames_is_concatenated():
    engine = _FakeSTT()
    _recognize(engine, [_frame(n=800), _frame(n=800)])
    assert engine.seen.size == 1600


def test_48k_frame_reaches_engine_as_16k_audio():
    """WebRTC room audio is commonly 48 kHz; the model is 16 kHz."""
    engine = _FakeSTT()
    _recognize(engine, _frame(n=4800, rate=48000))  # 100 ms
    assert engine.seen.dtype == np.float32
    assert engine.seen.size == 1600  # 100 ms at 16 kHz, not 4800


def test_16k_frame_passes_through_unchanged():
    engine = _FakeSTT()
    _recognize(engine, _frame(n=1600, value=16384, rate=16000))
    assert engine.seen.size == 1600
    np.testing.assert_allclose(engine.seen, 0.5)


def test_declared_sample_rate_is_read_not_assumed():
    """Same sample count at different declared rates -> different output length."""
    sizes = {}
    for rate in (8000, 16000, 24000, 48000):
        engine = _FakeSTT()
        _recognize(engine, _frame(n=4800, rate=rate))
        sizes[rate] = engine.seen.size
    assert sizes[16000] == 4800
    assert sizes[8000] == pytest.approx(9600, abs=8)  # upsampled
    assert sizes[24000] == pytest.approx(3200, abs=8)
    assert sizes[48000] == pytest.approx(1600, abs=8)


def test_resampling_preserves_pitch_not_just_length():
    """A 1 kHz tone must still be 1 kHz at 16 kHz (rules out slicing/relabelling)."""
    engine = _FakeSTT()
    _recognize(engine, _tone_frame(1000, 48000))
    audio = engine.seen
    spectrum = np.abs(np.fft.rfft(audio))
    peak_hz = np.argmax(spectrum) * 16000 / audio.size
    assert abs(peak_hz - 1000) < 20


def test_multichannel_frame_is_reduced_to_mono():
    engine = _FakeSTT()
    _recognize(engine, _frame(n=1600, value=1000, rate=16000, channels=2))
    assert engine.seen.ndim == 1
    assert engine.seen.size == 1600


def test_multichannel_48k_is_mono_and_resampled():
    engine = _FakeSTT()
    _recognize(engine, _frame(n=4800, rate=48000, channels=2))
    assert engine.seen.ndim == 1
    assert engine.seen.size == pytest.approx(1600, abs=8)
