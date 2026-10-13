"""Carry PCM from a producer thread to an async consumer, with backpressure.

QueuedOnnxTTS (backends/tts.py) synthesises on a long-lived worker thread and
emits PCM through a callback. LiveKit's TTS interface consumes an async
iterator. This module is the only place that boundary is crossed, so the
threading rules live here rather than being restated at each call site.

Imports no LiveKit packages on purpose: the risky part is the thread/asyncio
crossing, and it should be testable without the `livekit` extra installed.

Design notes, each a decision rather than an accident:

*Backpressure, not eviction.* When the consumer falls behind, push() blocks the
producer thread. The obvious alternative -- an unbounded queue, or dropping the
oldest frame -- is what hmi_qt/nova_hmi/audio.py does on its uplink, and it
loses audio silently with nothing signalled to the far side. A synthesiser may
be slowed down; its output may not be quietly discarded.

*Failures travel forward.* A synth exception is handed to the consumer and
re-raised there. Swallowing it would leave a turn hanging with no audio and no
error, which is the hardest kind of fault to diagnose from a log.

*Aborts travel backward.* LiveKit cancels TTS when the user barges in. The
producer learns via ProducerClosed from its next push() and can stop, rather
than synthesising a turn nobody will hear.
"""
from __future__ import annotations

import asyncio
import threading
from typing import AsyncIterator

__all__ = ["PcmBridge", "ProducerClosed"]


class ProducerClosed(RuntimeError):
    """Raised inside the producer thread when the consumer has gone away."""


class _End:
    """End-of-stream sentinel. A private class so no PCM value can collide."""

    __slots__ = ()


_END = _End()


class PcmBridge:
    """One-way PCM channel: producer thread -> asyncio consumer.

    The producer thread calls push(), then exactly one of close() or fail().
    The consumer awaits `async for chunk in bridge`.

    Every method below records which side it is called from; getting that wrong
    is the one way to misuse this class.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, *, maxsize: int = 32) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._loop = loop
        # Unbounded on purpose. The semaphore below is the only bound; giving
        # the queue one as well is not belt-and-braces, it is a race. push()
        # schedules the put via call_soon_threadsafe, so the slot is reserved
        # before it is consumed -- several pushes can be in flight at once,
        # overflow the queue's own bound, and raise QueueFull *inside* a loop
        # callback where it is logged but not propagated. The chunk is then
        # silently lost, and when the lost chunk is the _END sentinel the
        # consumer waits forever.
        self._queue: asyncio.Queue = asyncio.Queue()
        # The real backpressure: acquired before a push is scheduled, released
        # once the consumer has taken an item.
        self._slots = threading.Semaphore(maxsize)
        self._closed = threading.Event()
        self._error: BaseException | None = None

    # ---- consumer side (event loop) ---------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._queue.get()
            self._slots.release()
            if item is _END:
                if self._error is not None:
                    raise self._error
                return
            yield item

    def abort(self) -> None:
        """Stop the stream from the consumer side (barge-in, cancellation).

        Safe to call more than once. Releases the semaphore so a producer
        already blocked in push() wakes and sees the closed flag, rather than
        deadlocking on a queue nobody will drain.
        """
        if self._closed.is_set():
            return
        self._closed.set()
        self._slots.release()

    def cancel(self) -> None:
        """Stop both sides on behalf of a third party (event-loop thread only).

        abort() assumes the consumer has stopped iterating itself. When someone
        else ends the stream -- a newer turn superseding this one -- the
        consumer is still waiting in `async for`, so it must be handed the end
        sentinel as well as the producer being released. Idempotent.
        """
        if self._closed.is_set():
            return
        self.abort()
        self._queue.put_nowait(_END)

    # ---- producer side (worker thread) ------------------------------------

    def push(self, pcm: bytes) -> None:
        """Hand one chunk to the consumer. Blocks while the queue is full.

        Raises ProducerClosed if the consumer aborted, either before the call
        or while this call was blocked waiting for a slot.
        """
        if self._closed.is_set():
            raise ProducerClosed("consumer has closed the stream")
        self._slots.acquire()
        if self._closed.is_set():
            # Aborted while we waited. Pass the slot on so any other blocked
            # producer also wakes and observes the close.
            self._slots.release()
            raise ProducerClosed("consumer closed the stream while waiting")
        self._loop.call_soon_threadsafe(self._queue.put_nowait, pcm)

    def close(self) -> None:
        """Signal a normal end of stream. Idempotent."""
        self._finish(None)

    def fail(self, exc: BaseException) -> None:
        """End the stream and re-raise `exc` in the consumer. Idempotent."""
        self._finish(exc)

    def _finish(self, exc: BaseException | None) -> None:
        if self._closed.is_set():
            return
        self._error = exc
        self._closed.set()
        # The sentinel bypasses the semaphore: the consumer must always be able
        # to observe termination even when every slot is spoken for.
        self._loop.call_soon_threadsafe(self._queue.put_nowait, _END)
