"""NovaAgent: the real Nova pipeline behind a LiveKit room.

PCM passthrough. The pipeline runs a text turn exactly as it does for
/v1/realtime -- router, tools, Groq <-> Hailo fallback, its own Piper and phrase
cache, local_play=False -- and hands its audio out through on_audio_pcm. This
agent carries that audio into session.say(), so LAN and online callers hear the
same Nova. LiveKit owns the room, VAD turns and interruption; Nova owns the
answer.

One pipeline turn at a time: the pipeline has one TTS queue and one history,
so a newer user turn supersedes (aborts and awaits) the running one.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import numpy as np
from livekit import rtc
from livekit.agents import Agent, ChatContext, ChatMessage, StopResponse

from nova_hailo.lk.bridge import PcmBridge, ProducerClosed
from nova_hailo.metrics import TurnMetrics
from nova_hailo.web.cards import delivery_card_events
from nova_hailo.web.research_speech import delivery_trace_events, research_announcement

logger = logging.getLogger("nova.lk")

# 100 ms frames: small enough for LiveKit's 200 ms AudioSource queue and prompt
# interruption, large enough not to flood the event loop.
_FRAME_S = 0.1
# How long a superseded turn may take to leave the pipeline. Past this the
# pipeline thread is stuck; starting another turn would run two at once.
_SUPERSEDE_WAIT_S = 10.0
# Same cadence as the LAN door's research tick (realtime_session.RESEARCH_TICK_SEC).
_RESEARCH_TICK_S = 2.0
_QUIET_AGENT_STATES = frozenset({"listening", "idle"})
_HOOKS = ("on_audio_pcm", "on_validated_clause", "on_tool_status", "on_token", "on_research_status",
          "on_agent_trace")


def _frames_of(pcm: np.ndarray, rate: int) -> list[rtc.AudioFrame]:
    data = (np.clip(np.asarray(pcm, np.float32).reshape(-1), -1.0, 1.0) * 32767.0).astype(np.int16)
    step = max(1, int(rate * _FRAME_S))
    return [
        rtc.AudioFrame(
            data=data[i : i + step].tobytes(),
            sample_rate=rate,
            num_channels=1,
            samples_per_channel=len(data[i : i + step]),
        )
        for i in range(0, len(data), step)
    ]


async def _audio_stream(bridge: PcmBridge) -> AsyncIterator[rtc.AudioFrame]:
    """Frames at one rate. LiveKit sizes its resampler from the first frame
    only, so a later chunk at another rate is resampled here rather than played
    at the wrong pitch."""
    base: int | None = None
    resamplers: dict[int, rtc.AudioResampler] = {}
    async for pcm, rate in bridge:
        if base is None:
            base = rate
        for frame in _frames_of(pcm, rate):
            if rate == base:
                yield frame
                continue
            rs = resamplers.get(rate)
            if rs is None:
                rs = resamplers[rate] = rtc.AudioResampler(input_rate=rate, output_rate=base)
            for out in rs.push(frame):
                yield out
    for rs in resamplers.values():
        for out in rs.flush():
            yield out


async def _text_stream(q: asyncio.Queue) -> AsyncIterator[str]:
    while (clause := await q.get()) is not None:
        yield clause + " "


class NovaAgent(Agent):
    def __init__(
        self,
        pipeline: Any,
        *,
        on_event: Callable[[dict], None] | None = None,
        observe: Any = None,
    ) -> None:
        super().__init__(instructions="You are Nova.")
        self._pipeline = pipeline
        self._on_event = on_event
        # Displays on /v1/observe (lk.observe_mirror.ObserveMirror); None = not mirrored.
        self._observe = observe
        self._turn: asyncio.Task | None = None
        self._bridge: PcmBridge | None = None
        self._handle: Any = None
        self._research_backlog: list[dict] = []
        self._research_task: asyncio.Task | None = None

    # ---- LiveKit hooks -----------------------------------------------------

    async def on_user_turn_completed(self, turn_ctx: ChatContext, new_message: ChatMessage) -> None:
        text = (new_message.text_content or "").strip()
        if text and await self._supersede_running_turn():
            self._start_turn(text)
        # Nova answers through the pipeline; LiveKit must not attempt an LLM reply.
        raise StopResponse()

    async def on_enter(self) -> None:
        self._research_task = asyncio.create_task(self._research_loop())

    async def on_exit(self) -> None:
        if self._research_task is not None:
            self._research_task.cancel()
        await self._supersede_running_turn()

    async def wait_idle(self) -> None:
        if self._turn is not None:
            await asyncio.shield(self._turn)

    # ---- turn lifecycle ----------------------------------------------------

    def _emit(self, msg: dict) -> None:
        for sink in (self._on_event, self._observe and self._observe.event):
            if sink is None:
                continue
            try:
                sink(msg)
            except Exception:
                logger.exception("nova event sink failed for %s", msg.get("type"))

    def _abort_current(self) -> None:
        """Stop the running turn: its audio, its speech and the pipeline itself."""
        if self._bridge is not None:
            self._bridge.cancel()
        interrupt = getattr(self._handle, "interrupt", None)
        if callable(interrupt) and not getattr(self._handle, "done", lambda: False)():
            try:
                interrupt(force=True)
            except Exception:
                logger.debug("speech handle interrupt failed", exc_info=True)
        self._pipeline.on_abort()

    async def _supersede_running_turn(self) -> bool:
        """Abort and await the running turn. False if it will not leave the pipeline."""
        turn = self._turn
        if turn is None or turn.done():
            return True
        self._abort_current()
        try:
            await asyncio.wait_for(asyncio.shield(turn), _SUPERSEDE_WAIT_S)
        except asyncio.TimeoutError:
            logger.error(
                "previous pipeline turn still running %.0fs after abort; dropping the new turn",
                _SUPERSEDE_WAIT_S,
            )
            return False
        return True

    def _start_turn(self, text: str) -> None:
        gen = self._observe.turn_started(text) if self._observe is not None else None
        self._start_speech(lambda: self._pipeline.run_text_turn(text), label=text, generation=gen)

    # ---- async deep_research delivery ---------------------------------------

    def _quiet(self) -> bool:
        """Nobody is talking and no turn owns the pipeline."""
        if self._turn is not None and not self._turn.done():
            return False
        session = self.session
        return (
            getattr(session, "user_state", "listening") != "speaking"
            and getattr(session, "agent_state", "listening") in _QUIET_AGENT_STATES
        )

    async def deliver_research_once(self) -> bool:
        """Speak one finished deep_research result if the room is quiet.

        deep_research runs as a background job: the turn that starts it only
        says "this may take a moment". Without this, a finished result is never
        spoken and every "did you find it?" starts another job. A result that
        cannot be said yet stays queued for the next quiet moment.
        """
        drain = getattr(self._pipeline, "drain_finished_research", None)
        if drain is None or not self._quiet():
            return False
        if not self._research_backlog:
            self._research_backlog = list(await asyncio.to_thread(drain))
        if not self._quiet():
            return False
        if not self._research_backlog:
            return await self._deliver_guidance_once()
        item = self._research_backlog.pop(0)
        line = research_announcement(item)
        if not line:
            return False
        pipe = self._pipeline

        def speak():
            pipe.begin_turn()  # clear a stale abort/barge-in before speaking
            return pipe._speak_tool_line(
                line,
                wall0=time.perf_counter(),
                metrics=TurnMetrics(notes=["research_delivery"]),
                user_text=str(item.get("user_text") or ""),
                auth_decision="n/a",
                tool_name="deep_research",
                record_session=False,
                add_history=True,
                research_job_id=str(item.get("job_id") or ""),
            )

        for event in delivery_trace_events(item, None) + delivery_card_events(item, None):
            self._emit(event)
        logger.info("research: delivering job=%s", str(item.get("job_id"))[:12])
        # Interrupted mid-delivery means the user talked over it on purpose;
        # it is not re-queued.
        self._start_speech(speak, label="research")
        return True

    async def _deliver_guidance_once(self) -> bool:
        """One turn-by-turn line, only when nothing else is waiting to be said."""
        if not self._quiet():
            return False
        feed = getattr(self._pipeline, "nav_feed", None)
        if feed is None or not getattr(feed, "guidance_waiting", lambda: False)():
            return False
        if not self._quiet():
            return False
        line = feed.take_guidance()
        if not line:
            return False
        pipe = self._pipeline

        def speak():
            pipe.begin_turn()
            return pipe._speak_tool_line(
                line,
                wall0=time.perf_counter(),
                metrics=TurnMetrics(notes=["nav_guidance"]),
                user_text="",
                auth_decision="n/a",
                tool_name="navigation",
                record_session=False,
                add_history=True,
            )

        self._start_speech(speak, label="nav")
        return True

    async def _research_loop(self) -> None:
        while True:
            await asyncio.sleep(_RESEARCH_TICK_S)
            try:
                await self.deliver_research_once()
            except Exception:
                logger.exception("research delivery failed")

    # ---- one spoken reply from the pipeline --------------------------------

    def _start_speech(
        self, run_pipeline: Callable[[], Any], *, label: str, generation: int | None = None
    ) -> None:
        loop = asyncio.get_running_loop()
        pipe = self._pipeline
        bridge = PcmBridge(loop)
        text = label
        clauses: asyncio.Queue = asyncio.Queue()

        started = time.perf_counter()
        first_audio: list[float] = []

        def on_pcm(audio: np.ndarray, rate: int) -> None:
            if not first_audio:
                first_audio.append(time.perf_counter())
                logger.info(
                    "latency nova: turn committed -> first audio %.0fms",
                    (first_audio[0] - started) * 1000,
                )
            try:
                bridge.push((audio, int(rate)))
            except ProducerClosed:
                pass  # turn aborted; the pipeline stops on its own abort flag

        def on_clause(clause: str) -> None:
            loop.call_soon_threadsafe(clauses.put_nowait, clause)

        def on_tool(payload: dict) -> None:
            loop.call_soon_threadsafe(self._emit, {"type": "nova.tool_status", **payload})

        observe = self._observe if generation is not None else None

        def on_trace(payload: dict) -> None:
            loop.call_soon_threadsafe(observe.trace, generation, payload)

        def run():
            # Same wiring shape as realtime_session: hooks live only for this turn.
            pipe.on_audio_pcm = on_pcm
            pipe.on_validated_clause = on_clause
            pipe.on_tool_status = on_tool
            pipe.on_token = None
            pipe.on_research_status = None
            pipe.on_agent_trace = on_trace if observe is not None else None
            try:
                return run_pipeline()
            finally:
                for name in _HOOKS:
                    setattr(pipe, name, None)

        async def drive() -> None:
            reply, tool_name = "", None
            try:
                result = await asyncio.to_thread(run)
                metrics = getattr(result, "metrics", None)
                if metrics is not None:
                    self._emit({"type": "nova.turn_metrics", **metrics.to_dict()})
                reply = str(getattr(result, "assistant_text", "") or "")
                tool_name = getattr(result, "tool_name", None)
            except Exception:
                # A failed turn ends its speech; it must never hang the room.
                logger.exception("pipeline turn failed for %r", text[:60])
            finally:
                bridge.close()
                clauses.put_nowait(None)
                if observe is not None:
                    observe.turn_finished(generation, text, reply, tool_name)

        def on_speech_done(handle: Any) -> None:
            if getattr(handle, "interrupted", False) and self._handle is handle:
                self._abort_current()

        self._bridge = bridge
        self._turn = asyncio.create_task(drive())
        self._handle = self.session.say(_text_stream(clauses), audio=_audio_stream(bridge))
        self._handle.add_done_callback(on_speech_done)
