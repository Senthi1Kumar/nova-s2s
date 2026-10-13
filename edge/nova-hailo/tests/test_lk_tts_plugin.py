"""PiperTTS behind LiveKit's TTS interface. No LiveKit server, no audio device.

ChunkedStream.__init__ schedules _run() itself (livekit/agents/tts/tts.py,
`self._synthesize_task = asyncio.create_task(...)`), so these tests drive the
stream through its public consumption path (collect() / async iteration) with
LiveKit's real AudioEmitter, never by calling _run() a second time.
"""
from __future__ import annotations

import asyncio
import importlib.util
import threading
import time

from types import SimpleNamespace

import numpy as np
import pytest

HAS_LIVEKIT = importlib.util.find_spec("livekit.agents") is not None
pytestmark = pytest.mark.skipif(not HAS_LIVEKIT, reason="livekit extra not installed")


class _FakeEngine:
    """Stands in for PiperTTS: yields (float32, sample_rate) and honours interrupt."""

    def __init__(self, chunks=3, sample_rate=22050, boom=False):
        self._chunks, self._sr, self._boom = chunks, sample_rate, boom
        self.interrupted = False
        # Same shape as PiperTTS: the voice config carries the true rate.
        self._voice = SimpleNamespace(config=SimpleNamespace(sample_rate=sample_rate))

    def synthesize_stream(self, text: str):
        if self._boom:
            yield np.full(160, 0.5, dtype=np.float32), self._sr
            raise RuntimeError("synth exploded")
        for i in range(self._chunks):
            if self.interrupted:
                return
            yield np.full(160, (i + 1) / 10.0, dtype=np.float32), self._sr

    def interrupt(self):
        self.interrupted = True


def _collect(engine, text="hello", **tts_kwargs):
    """Run a synthesis to completion through the public path.

    Returns (pcm bytes, sample_rate, num_channels). Iterates rather than using
    collect(), which raises on a stream that produced no frames.
    """
    from nova_hailo.lk.tts_plugin import NovaTTS

    async def _go():
        stream = NovaTTS(engine, **tts_kwargs).synthesize(text)
        frames = []
        try:
            async for ev in stream:
                frames.append(ev.frame)
        finally:
            await stream.aclose()
        return frames

    frames = asyncio.run(_go())
    pcm = b"".join(bytes(f.data) for f in frames)
    rate = frames[0].sample_rate if frames else None
    channels = frames[0].num_channels if frames else None
    return pcm, rate, channels


def _voiced(pcm: bytes) -> np.ndarray:
    """int16 samples with LiveKit's zero padding of the final frame removed.

    AudioEmitter pads the last frame up to its frame size, so the byte total is
    not exactly what was pushed; the test signal is non-zero, so trimming
    trailing zeros recovers it.
    """
    return np.trim_zeros(np.frombuffer(pcm, dtype=np.int16), trim="b")


def test_audio_reaches_the_emitter_at_the_declared_rate():
    """Review Focus 5: what we declare must equal what we push, or pitch is wrong."""
    pcm, rate, channels = _collect(_FakeEngine(chunks=3))
    assert rate == 22050
    assert channels == 1
    assert _voiced(pcm).size == 3 * 160, "3 chunks x 160 int16 samples, none lost"


def test_empty_text_ends_cleanly_without_hanging():
    """Review Focus 1."""
    pcm, _, _ = _collect(_FakeEngine(chunks=0), text="   ")
    assert pcm == b""


def test_engine_failure_surfaces_rather_than_truncating():
    """Review Focus 2: a silent truncation is the hardest fault to read in a log."""
    with pytest.raises(RuntimeError, match="synth exploded"):
        _collect(_FakeEngine(boom=True))


def test_float32_is_converted_to_int16_pcm():
    pcm, _, _ = _collect(_FakeEngine(chunks=1))
    samples = _voiced(pcm)
    assert samples.size == 160
    assert abs(int(samples[0]) - int(0.1 * 32767)) <= 1


def test_abort_interrupts_the_engine():
    """Review Focus 3: barge-in must stop synthesis, not just stop playback."""
    from nova_hailo.lk.tts_plugin import NovaTTS

    engine = _FakeEngine(chunks=100_000)

    async def _go():
        stream = NovaTTS(engine).synthesize("long")
        await asyncio.wait_for(stream.__anext__(), timeout=5)  # audio is flowing
        await stream.aclose()  # what LiveKit does on barge-in

    asyncio.run(_go())
    assert engine.interrupted is True, "the worker thread never learned about the abort"


class _StickyEngine:
    """Mirrors PiperTTS: interrupt() sets a flag that stays set until
    clear_interruption(), and synthesize_stream stops while it is set."""

    def __init__(self):
        self._interrupt = False
        self._voice = SimpleNamespace(config=SimpleNamespace(sample_rate=22050))

    def interrupt(self):
        self._interrupt = True

    def clear_interruption(self):
        self._interrupt = False

    def synthesize_stream(self, text: str):
        for i in range(2):
            if self._interrupt:
                return
            yield np.full(160, (i + 1) / 10.0, dtype=np.float32), 22050


def test_consecutive_turns_on_one_instance_both_produce_audio():
    """A completed turn must not leave the engine interrupted for the next one."""
    from nova_hailo.lk.tts_plugin import NovaTTS

    tts = NovaTTS(_StickyEngine())

    async def _turn():
        stream = tts.synthesize("hello")
        frames = []
        try:
            async for ev in stream:
                frames.append(ev.frame)
        finally:
            await stream.aclose()
        return b"".join(bytes(f.data) for f in frames)

    async def _go():
        return await _turn(), await _turn()

    first, second = asyncio.run(_go())
    assert _voiced(first).size == 2 * 160
    assert _voiced(second).size == 2 * 160, "second turn was silenced by a stale interrupt flag"


def test_stale_interrupt_flag_does_not_silence_a_new_turn():
    engine = _StickyEngine()
    engine.interrupt()  # left over from some earlier path
    pcm, _, _ = _collect(engine)
    assert _voiced(pcm).size == 2 * 160


def test_each_stream_gets_a_distinct_request_id():
    from nova_hailo.lk.tts_plugin import NovaTTS

    async def _ids(tts, text):
        stream = tts.synthesize(text)
        try:
            return {ev.request_id async for ev in stream}
        finally:
            await stream.aclose()

    async def _go():
        tts = NovaTTS(_FakeEngine())
        return await _ids(tts, "a"), await _ids(tts, "b")

    a, b = asyncio.run(_go())
    assert len(a) == 1 and len(b) == 1 and a != b


def test_pushed_rate_that_differs_from_declared_rate_fails_loudly():
    """Declaring one rate and pushing another plays at the wrong pitch."""
    with pytest.raises(ValueError, match=r"16000.*22050|22050.*16000"):
        _collect(_FakeEngine(sample_rate=16000), sample_rate=22050)


def test_declared_rate_is_derived_from_the_engine_voice_config():
    from nova_hailo.lk.tts_plugin import NovaTTS

    assert NovaTTS(_FakeEngine(sample_rate=16000)).sample_rate == 16000
    pcm, rate, _ = _collect(_FakeEngine(sample_rate=16000))
    assert rate == 16000
    assert _voiced(pcm).size == 3 * 160


def test_explicit_sample_rate_overrides_the_engine():
    from nova_hailo.lk.tts_plugin import NovaTTS

    assert NovaTTS(_FakeEngine(sample_rate=16000), sample_rate=24000).sample_rate == 24000


def test_undeterminable_rate_raises_at_construction():
    from nova_hailo.lk.tts_plugin import NovaTTS

    with pytest.raises(ValueError, match="sample"):
        NovaTTS(object())


class _ConcurrencyEngine:
    """Records how many synthesize_stream() bodies are inside the engine at once.

    PiperVoice is not safe to enter from two threads, and the interrupt flag is
    shared, so overlap is the fault under test."""

    def __init__(self):
        self._guard = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.order: list[tuple[str, str]] = []
        self._voice = SimpleNamespace(config=SimpleNamespace(sample_rate=22050))

    def synthesize_stream(self, text: str):
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.order.append(("enter", text))
        try:
            for i in range(4):
                time.sleep(0.02)
                yield np.full(160, (i + 1) / 10.0, dtype=np.float32), 22050
        finally:
            with self._guard:
                self.active -= 1
                self.order.append(("exit", text))


def test_overlapping_streams_on_one_tts_run_strictly_one_after_the_other():
    """One NovaTTS wraps one engine; two streams must never be inside it together."""
    from nova_hailo.lk.tts_plugin import NovaTTS

    engine = _ConcurrencyEngine()

    async def _drain(stream):
        try:
            return [ev.frame async for ev in stream]
        finally:
            await stream.aclose()

    async def _go():
        tts = NovaTTS(engine)
        first, second = tts.synthesize("first"), tts.synthesize("second")
        return await asyncio.gather(_drain(first), _drain(second))

    a, b = asyncio.run(_go())
    assert engine.max_active == 1, "two streams were inside the engine at the same time"
    assert engine.order == [
        ("enter", "first"), ("exit", "first"), ("enter", "second"), ("exit", "second"),
    ]
    assert _voiced(b"".join(bytes(f.data) for f in a)).size == 4 * 160
    assert _voiced(b"".join(bytes(f.data) for f in b)).size == 4 * 160


class _SlowStickyEngine(_StickyEngine):
    """_StickyEngine with per-chunk latency: a long text for barge-in, a short one after."""

    def synthesize_stream(self, text: str):
        for i in range(100_000 if text == "long" else 2):
            time.sleep(0.02)
            if self._interrupt:
                return
            yield np.full(160, (i + 1) / 10.0, dtype=np.float32), 22050


def test_late_interrupt_from_an_aborted_stream_does_not_silence_the_next():
    """Barge-in aborts stream A while stream B is already starting.

    Without per-engine serialisation B clears the sticky flag, then A's late
    interrupt() sets it, B yields nothing and LiveKit turns that into an
    APIError. max_retry=0 makes that first-attempt failure visible instead of
    letting a retry paper over it."""
    from livekit.agents.types import APIConnectOptions

    from nova_hailo.lk.tts_plugin import NovaTTS

    engine = _SlowStickyEngine()
    no_retry = APIConnectOptions(max_retry=0)

    async def _go():
        tts = NovaTTS(engine)
        a = tts.synthesize("long", conn_options=no_retry)
        await asyncio.wait_for(a.__anext__(), timeout=5)  # A is mid-synthesis
        closer = asyncio.ensure_future(a.aclose())  # barge-in, not yet delivered
        b = tts.synthesize("short", conn_options=no_retry)  # next turn starts at once
        try:
            frames = [ev.frame async for ev in b]
        finally:
            await b.aclose()
            await closer
        return frames

    frames = asyncio.run(_go())
    assert _voiced(b"".join(bytes(f.data) for f in frames)).size == 2 * 160


def test_producer_thread_that_outlives_the_join_bound_is_logged(monkeypatch, caplog):
    import logging

    from nova_hailo.lk import tts_plugin

    monkeypatch.setattr(tts_plugin, "_PRODUCER_JOIN_TIMEOUT_S", 0.05)
    release = threading.Event()

    class _Stuck(_FakeEngine):
        def synthesize_stream(self, text):
            yield np.full(22050, 0.5, dtype=np.float32), 22050  # 1 s: fills frames
            release.wait(5)  # stuck "inside ONNX"

    async def _go():
        stream = tts_plugin.NovaTTS(_Stuck()).synthesize("x")
        await asyncio.wait_for(stream.__anext__(), timeout=5)
        await stream.aclose()

    try:
        with caplog.at_level(logging.WARNING, logger="nova.lk.tts"):
            asyncio.run(_go())
    finally:
        release.set()
    assert any("still running" in r.getMessage() for r in caplog.records)


def test_disabled_engine_reports_a_load_failure_not_a_sample_rate_error():
    """PiperTTS sets enabled=False and _voice=None when the model fails to load."""
    from nova_hailo.lk.tts_plugin import NovaTTS

    class _Failed:
        enabled = False
        _voice = None

    with pytest.raises(RuntimeError, match="failed to load"):
        NovaTTS(_Failed())
    with pytest.raises(RuntimeError, match="failed to load"):
        NovaTTS(_Failed(), sample_rate=22050)
