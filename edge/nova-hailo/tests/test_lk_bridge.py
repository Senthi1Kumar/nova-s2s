"""Thread -> asyncio PCM bridge: ordering, backpressure, shutdown, errors.

Callers: pytest. Exercises nova_hailo.lk.bridge only -- no LiveKit, no audio
device, no models -- so it runs identically on the laptop and the Pi and does
not need the `livekit` extra installed. No date schemas.

Why this file exists before any LiveKit code: the async/threaded boundary is
the central technical risk of the LiveKit milestone. QueuedOnnxTTS
(backends/tts.py) emits PCM from a worker thread through a callback; LiveKit's
TTS interface consumes an async iterator. Everything else in the integration
rests on that crossing being correct, so it is specified here first.

Async tests follow the house pattern -- an inner coroutine driven by
asyncio.run() (see tests/test_barge_in.py:162, tests/test_realtime_device_api.py:149)
-- rather than pytest-asyncio, which this project does not depend on.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from nova_hailo.lk.bridge import PcmBridge, ProducerClosed


def _chunk(i: int) -> bytes:
    """Distinguishable 2-sample PCM16 frame, so ordering is checkable."""
    return i.to_bytes(2, "little") * 2


def test_chunks_arrive_in_order_and_none_are_dropped():
    """Silent reordering or loss would surface as garbled speech, not an error."""

    async def _go():
        bridge = PcmBridge(asyncio.get_running_loop(), maxsize=4)
        sent = [_chunk(i) for i in range(32)]

        def producer():
            for c in sent:
                bridge.push(c)
            bridge.close()

        threading.Thread(target=producer, name="test-producer", daemon=True).start()
        got = [c async for c in bridge]
        assert got == sent

    asyncio.run(_go())


def test_close_terminates_the_iterator():
    async def _go():
        bridge = PcmBridge(asyncio.get_running_loop(), maxsize=4)
        bridge.push(_chunk(1))
        bridge.close()
        got = [c async for c in bridge]
        assert got == [_chunk(1)]

    asyncio.run(_go())


def test_producer_blocks_when_full_rather_than_dropping():
    """Backpressure, not eviction.

    The HMI uplink queue drops its oldest frame on overflow and never signals
    it (hmi_qt/nova_hmi/audio.py). That is silent audio loss, and it is the
    behaviour this bridge must not repeat: a slow consumer has to slow the
    synthesiser down instead.
    """

    async def _go():
        bridge = PcmBridge(asyncio.get_running_loop(), maxsize=2)
        pushed: list[int] = []
        finished = threading.Event()

        def producer():
            for i in range(6):
                bridge.push(_chunk(i))
                pushed.append(i)
            finished.set()
            bridge.close()

        threading.Thread(target=producer, name="test-producer", daemon=True).start()

        # Give the producer room to run ahead, if it were going to.
        await asyncio.sleep(0.15)
        assert not finished.is_set(), "producer completed despite a full queue"
        assert len(pushed) <= 3, f"producer over-ran the bound: pushed={pushed}"

        got = [c async for c in bridge]
        assert got == [_chunk(i) for i in range(6)]
        assert finished.is_set()

    asyncio.run(_go())


def test_producer_exception_surfaces_to_the_consumer():
    """A synth failure must raise here, not hang the turn or truncate silently."""

    async def _go():
        bridge = PcmBridge(asyncio.get_running_loop(), maxsize=4)

        def producer():
            bridge.push(_chunk(0))
            bridge.fail(RuntimeError("synth exploded"))

        threading.Thread(target=producer, name="test-producer", daemon=True).start()

        got = []
        with pytest.raises(RuntimeError, match="synth exploded"):
            async for c in bridge:
                got.append(c)
        assert got == [_chunk(0)], "chunks before the failure are still delivered"

    asyncio.run(_go())


def test_consumer_abort_tells_the_producer_to_stop():
    """LiveKit cancels TTS on barge-in; the worker thread must learn about it.

    Without this the synthesiser keeps generating audio for a turn the user
    already interrupted -- the same waste GenerationGuard exists to prevent on
    the existing path.
    """

    async def _go():
        bridge = PcmBridge(asyncio.get_running_loop(), maxsize=2)
        observed_closed = threading.Event()

        def producer():
            for i in range(1000):
                try:
                    bridge.push(_chunk(i))
                except ProducerClosed:
                    observed_closed.set()
                    return
                time.sleep(0.001)

        threading.Thread(target=producer, name="test-producer", daemon=True).start()

        it = bridge.__aiter__()
        await it.__anext__()
        bridge.abort()

        assert observed_closed.wait(timeout=2.0), "producer never saw the abort"
        assert bridge.closed is True

    asyncio.run(_go())


def test_push_after_close_raises_rather_than_silently_discarding():
    async def _go():
        bridge = PcmBridge(asyncio.get_running_loop(), maxsize=2)
        bridge.close()
        with pytest.raises(ProducerClosed):
            bridge.push(_chunk(0))

    asyncio.run(_go())


def test_third_party_cancel_ends_the_consumer_and_frees_a_blocked_producer():
    async def _go():
        loop = asyncio.get_running_loop()
        bridge = PcmBridge(loop, maxsize=1)
        outcome: list[str] = []

        def produce():
            try:
                while True:
                    bridge.push(b"x")
            except ProducerClosed:
                outcome.append("stopped")

        consumed: list[bytes] = []

        async def consume():
            async for chunk in bridge:
                consumed.append(chunk)
                await asyncio.sleep(0.05)  # slow consumer: producer stays blocked

        t = threading.Thread(target=produce, daemon=True)
        t.start()
        reader = asyncio.create_task(consume())
        await asyncio.sleep(0.2)
        bridge.cancel()
        await asyncio.wait_for(reader, 2)  # the consumer is not left waiting
        await asyncio.to_thread(t.join, 2)
        assert outcome == ["stopped"] and not t.is_alive()
        bridge.cancel()  # idempotent

    asyncio.run(_go())
