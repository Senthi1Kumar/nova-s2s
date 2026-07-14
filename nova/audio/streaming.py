"""Async sentence-level streaming synthesis (Vui's chunk-and-stream pattern)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import numpy as np

from nova.audio.sentencer import SentenceChunker

_END = object()


class SynthesisCancelled(Exception):
    """Raised by a synthesize callable that aborted mid-pipeline because a
    cancellation check (barge-in / turn-taking) fired. Defined here rather
    than in nova.engine.tts so this pure-orchestration module stays free of
    engine imports while both sides share the contract."""


class StreamingSynthesizer:
    def __init__(
        self,
        synthesize: Callable[[str], np.ndarray],
        *,
        max_pending: int = 4,
    ):
        self._synthesize = synthesize
        self.max_pending = max_pending

    async def stream(
        self,
        deltas: AsyncIterator[str],
        *,
        is_cancelled: Callable[[], bool] = lambda: False,
    ) -> AsyncIterator[np.ndarray]:
        loop = asyncio.get_running_loop()
        chunker = SentenceChunker()
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.max_pending)

        async def synth_one(text: str) -> bool:
            if is_cancelled():
                return False
            try:
                audio = await loop.run_in_executor(None, self._synthesize, text)
            except SynthesisCancelled:
                # The synthesize callable observed a barge-in/turn-taking
                # cancellation mid-pipeline -- stop the stream entirely.
                return False
            except ValueError:
                # A single chunk failing synthesis (e.g. it overran a
                # phoneme/token budget) shouldn't kill the whole stream --
                # skip it and keep going, per the M2/M3 harness principle
                # of not letting one bad turn take down the voice loop.
                return True
            if is_cancelled():
                return False
            await queue.put(audio)
            return True

        async def producer() -> None:
            try:
                async for delta in deltas:
                    if is_cancelled():
                        return
                    for text in chunker.push(delta):
                        if not await synth_one(text):
                            return
                tail = chunker.flush()
                if tail:
                    await synth_one(tail)
            finally:
                await queue.put(_END)

        task = asyncio.ensure_future(producer())
        try:
            while True:
                item = await queue.get()
                if item is _END:
                    break
                yield item
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
