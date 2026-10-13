"""LiveKit agent worker: the Pi joins a room and answers with local models.

Milestone 1 deliberately omits tools, wake word, the session FSM and barge-in.
The point is to prove NAT traversal, the thread/asyncio bridge, and that audio
survives the Pi's stack -- the three risks that would end this project.
"""
from __future__ import annotations

import logging

from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    ChatContext,
    ChatMessage,
    JobContext,
    JobProcess,
    StopResponse,
    cli,
)
from livekit.plugins import silero

from nova_hailo.backends.parakeet_stt import ParakeetSTT, resolve_parakeet_paths
from nova_hailo.backends.tts import create_tts
from nova_hailo.config import ROOT, AppConfig
from nova_hailo.lk.stt_plugin import NovaSTT
from nova_hailo.lk.tts_plugin import NovaTTS

logger = logging.getLogger("nova.lk")


def _build_stt(cfg: AppConfig) -> ParakeetSTT:
    """Same construction the pipeline uses (pipeline.py:888).

    ParakeetSTT takes gguf_path and lib_path positionally -- ParakeetSTT() with
    no arguments raises TypeError. resolve_parakeet_paths() turns the two config
    keys into real paths, so the agent and the pipeline cannot drift apart.
    """
    model, lib = resolve_parakeet_paths(
        cfg.get("model", "parakeet_model"),
        cfg.get("model", "parakeet_lib"),
    )
    if not (model and lib):
        raise RuntimeError(
            "parakeet model/lib not found; check model.parakeet_model and "
            "model.parakeet_lib in the active profile"
        )
    return ParakeetSTT(
        model,
        lib,
        decoder=str(cfg.get("model", "parakeet_decoder", default="tdt")),
        language=cfg.get("model", "parakeet_language"),
    )


def _build_tts(cfg: AppConfig):
    piper = cfg.get("model", "piper_onnx") or None
    if piper and not str(piper).startswith("/"):
        piper = str(ROOT / piper)
    # local_play=False: LiveKit carries the audio, so this engine must never
    # open the Pi's speaker. Only synthesize_stream() is used.
    return create_tts("piper", piper_path=piper, local_play=False)


class EchoAgent(Agent):
    """Speaks back what it heard. No LLM.

    Without an LLM nothing generates a reply, so a plain Agent would transcribe
    the user and then sit silent -- Milestone 1's goal, hearing a reply, would be
    unreachable. Echoing the transcript through TTS proves STT and TTS end to end
    and isolates the audio path from any model behaviour, which is exactly what
    this milestone tests.

    on_user_turn_completed is a documented lifecycle hook; StopResponse stops the
    session attempting a (non-existent) LLM reply afterwards.
    """

    def __init__(self) -> None:
        super().__init__(instructions="You are Nova.")

    async def on_enter(self) -> None:
        # Speak before any user turn: proves Piper -> LiveKit -> browser on its
        # own, so a silent room points at the input side (VAD/STT), not output.
        self.session.say("Hi, this is Nova. Say something and I'll repeat it.")

    async def on_user_turn_completed(
        self, turn_ctx: ChatContext, new_message: ChatMessage
    ) -> None:
        heard = (new_message.text_content or "").strip()
        # AgentSession.say() is synchronous and returns a SpeechHandle; it only
        # schedules the speech. Not awaited: awaiting the handle would hold this
        # turn hook open until playout finishes.
        self.session.say(f"You said: {heard}" if heard else "I didn't catch that.")
        raise StopResponse()


def _log_session_events(session: AgentSession) -> None:
    """Log each stage of a turn at INFO: VAD -> STT -> agent speaking.

    The SDK logs none of these at INFO, so a turn that dies silently leaves no
    trace of which stage stopped it.
    """
    session.on("user_state_changed", lambda ev: logger.info(
        "user %s -> %s", ev.old_state, ev.new_state))
    session.on("user_input_transcribed", lambda ev: logger.info(
        "stt final=%s text=%r", ev.is_final, ev.transcript))
    session.on("agent_state_changed", lambda ev: logger.info(
        "agent %s -> %s", ev.old_state, ev.new_state))
    session.on("error", lambda ev: logger.error(
        "session error source=%s: %r", type(ev.source).__name__, ev.error))
    session.on("metrics_collected", _log_eou_metrics)


def _log_eou_metrics(ev) -> None:
    """The turn-taking share of latency: speech end -> turn committed."""
    m = getattr(ev, "metrics", None)
    if type(m).__name__ != "EOUMetrics":
        return
    logger.info(
        "latency eou: end_of_utterance=%.0fms transcription=%.0fms",
        (m.end_of_utterance_delay or 0) * 1000,
        (m.transcription_delay or 0) * 1000,
    )


def single_job_load(server: AgentServer) -> float:
    """Worker load for the SDK's availability check: 1.0 while a job runs, else 0.0.

    The SDK's default load is CPU utilisation, which says nothing about memory.
    Each job process holds its own Parakeet, Piper and Silero, so the Pi's limit
    is the number of jobs, not CPU. Called by the SDK with the server
    (worker.py:1348-1357); load_fnc runs in an executor thread.
    """
    return 1.0 if server.active_jobs else 0.0


def prewarm(proc: JobProcess) -> None:
    """Load every model once per process, before any job is assigned.

    Runs in the job process at start-up (worker.py:670, job_proc_lazy_main.py:230),
    not on the entrypoint's event loop, so a caller never waits in a silent room
    for the load and the loop is never frozen by it. The entrypoint reads these
    from proc.userdata (job.py:1032) and loads nothing.
    """
    cfg = AppConfig.load()
    proc.userdata["stt"] = NovaSTT(_build_stt(cfg))
    proc.userdata["tts"] = NovaTTS(_build_tts(cfg))
    proc.userdata["vad"] = silero.VAD.load()


# Capacity policy for an 8 GB Pi that also carries the Hailo path.
#
# LiveKit runs every job in its own process, and each process loads its own
# models (see prewarm). The SDK defaults do not bound that: load_threshold is
# infinite in dev mode and 0.7 of CPU in prod (worker.py:168), and
# num_idle_processes is 0 in dev and the CPU count in prod (worker.py:319), so a
# second room or a reconnect storm could load the models several times over.
# These values are explicit, so they hold in both modes, but `start` is the mode
# for the Pi; `dev` is for a laptop and adds reloading on top.
#
#   setup_fnc=prewarm         the per-process model load (worker.py:347).
#   num_idle_processes=1      one warm process, not one per core (worker.py:332).
#   load_fnc=single_job_load  a running job fills the worker (worker.py:348).
#   load_threshold=0.5        below 1.0, so one running job is "full"; with no
#                             job the SDK's per-slot estimate is threshold / idle
#                             = 0.5, so a second concurrent request is refused
#                             too (worker.py:1372-1397). It also stops the SDK
#                             warming a spare while a job runs (worker.py:870-881).
#   initialize_process_timeout  the SDK kills a process whose prewarm exceeds 10 s
#                             (worker.py:335); loading three models on a Pi
#                             takes far longer.
server = AgentServer(
    setup_fnc=prewarm,
    load_fnc=single_job_load,
    load_threshold=0.5,
    num_idle_processes=1,
    initialize_process_timeout=120.0,
)


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    models = ctx.proc.userdata
    await ctx.connect()
    session = AgentSession(
        # NovaSTT is non-streaming, so the session wraps it with this VAD to
        # segment speech before calling recognize(). Required, not optional.
        stt=models["stt"],
        # llm is deliberately omitted, not passed as None. Its annotation is
        # NotGivenOr[LLM | RealtimeModel | DuplexModel | ... | str] -- None is
        # not a member, unlike vad, which is NotGivenOr[VAD | None]. Neither
        # form raises at construction, so only the annotation says which is
        # correct. The LLM arrives with the tools, after Milestone 1.
        tts=models["tts"],
        vad=models["vad"],
    )
    _log_session_events(session)
    await session.start(agent=EchoAgent(), room=ctx.room)
    logger.info("nova agent joined room=%s", ctx.room.name)


if __name__ == "__main__":
    cli.run_app(server)
