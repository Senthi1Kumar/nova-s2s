"""NovaAgent: drives the real NovaPipeline from a LiveKit room (PCM passthrough).

The pipeline and session are fakes with the same hook surface as NovaPipeline
(on_audio_pcm / on_validated_clause / on_tool_status, run_text_turn, on_abort)
and AgentSession.say(text_stream, audio=frames). No LiveKit server, no models.
"""
import asyncio
import threading
import time

import numpy as np
import pytest

pytest.importorskip("livekit.agents")

from livekit.agents import StopResponse  # noqa: E402

from nova_hailo.lk import nova_agent as na  # noqa: E402


class _Metrics:
    def to_dict(self):
        return {"ttfa_ms": 123.0}


class _Result:
    metrics = _Metrics()


class FakePipeline:
    """Speaks `script`: a list of ("clause", text) / ("pcm", seconds, rate) steps."""

    def __init__(self, script=None, *, block_until_abort=False, fail=None):
        self.script = script or [("clause", "Hi there."), ("pcm", 0.25, 16000)]
        self.block_until_abort = block_until_abort
        self.fail = fail
        self.on_audio_pcm = None
        self.on_validated_clause = None
        self.on_tool_status = None
        self.on_token = None
        self.on_research_status = None
        self.aborted = threading.Event()
        self.turns: list[str] = []
        self.running = 0
        self.max_running = 0
        self._lock = threading.Lock()

    def run_text_turn(self, text):
        with self._lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            self.turns.append(text)
            self.aborted.clear()
            if self.fail:
                raise RuntimeError(self.fail)
            for step in self.script:
                if step[0] == "clause":
                    self.on_validated_clause(step[1])
                else:
                    _, secs, rate = step
                    self.on_audio_pcm(np.full(int(secs * rate), 0.1, np.float32), rate)
            if self.on_tool_status:
                self.on_tool_status({"name": "weather", "status": "done"})
            if self.block_until_abort:
                # Keep pushing like a long answer until the agent aborts us.
                while not self.aborted.is_set():
                    self.on_audio_pcm(np.full(1600, 0.1, np.float32), 16000)
            return _Result()
        finally:
            with self._lock:
                self.running -= 1

    def on_abort(self):
        self.aborted.set()

    # ---- async research (same surface as NovaPipeline) --------------------
    finished: list = []
    spoken_research: list = []

    def drain_finished_research(self):
        out, self.finished = list(self.finished), []
        return out

    def begin_turn(self):
        self.aborted.clear()

    def _speak_tool_line(self, speak, **kw):
        self.spoken_research.append((speak, kw))
        self.on_validated_clause(speak)
        self.on_audio_pcm(np.full(1600, 0.1, np.float32), 16000)
        return _Result()


class FakeHandle:
    def __init__(self):
        self.interrupted = False
        self._cbs = []

    def add_done_callback(self, cb):
        self._cbs.append(cb)

    def finish(self, *, interrupted=False):
        self.interrupted = interrupted
        for cb in self._cbs:
            cb(self)


class FakeSession:
    """Consumes say()'s streams like LiveKit's forwarding tasks do."""

    def __init__(self, *, consume_audio=True):
        self.consume_audio = consume_audio
        self.user_state = "listening"
        self.agent_state = "listening"
        self.said: list[dict] = []
        self.tasks: list[asyncio.Task] = []

    def say(self, text, *, audio):
        handle = FakeHandle()
        rec = {"text": [], "frames": [], "handle": handle}
        self.said.append(rec)

        async def pump():
            async def texts():
                async for t in text:
                    rec["text"].append(t)

            async def frames():
                if not self.consume_audio:
                    return
                async for f in audio:
                    rec["frames"].append(f)

            await asyncio.gather(texts(), frames())
            handle.finish()

        self.tasks.append(asyncio.create_task(pump()))
        return handle


class _Msg:
    def __init__(self, text):
        self.text_content = text


def _agent(monkeypatch, pipeline, session, events=None):
    monkeypatch.setattr(na.NovaAgent, "session", property(lambda self: session))
    return na.NovaAgent(pipeline, on_event=(events.append if events is not None else None))


async def _turn(agent, text):
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(None, _Msg(text))


def test_turn_runs_pipeline_and_speaks_its_pcm_and_clauses(monkeypatch):
    async def go():
        pipe, sess, events = FakePipeline(), FakeSession(), []
        agent = _agent(monkeypatch, pipe, sess, events)
        await _turn(agent, "what's the weather")
        await agent.wait_idle()
        await asyncio.gather(*sess.tasks)
        return pipe, sess, events

    pipe, sess, events = asyncio.run(go())
    assert pipe.turns == ["what's the weather"]
    rec = sess.said[0]
    assert "".join(rec["text"]).strip() == "Hi there."
    assert rec["frames"] and all(f.sample_rate == 16000 for f in rec["frames"])
    assert sum(f.samples_per_channel for f in rec["frames"]) == 4000
    # hooks are the pipeline's, not ours, once the turn is over
    assert pipe.on_audio_pcm is None and pipe.on_validated_clause is None
    assert {"type": "nova.tool_status", "name": "weather", "status": "done"} in events
    assert {"type": "nova.turn_metrics", "ttfa_ms": 123.0} in events


def test_frames_are_split_small_for_the_audio_source(monkeypatch):
    async def go():
        pipe = FakePipeline([("pcm", 2.0, 16000)])
        sess = FakeSession()
        agent = _agent(monkeypatch, pipe, sess)
        await _turn(agent, "long")
        await agent.wait_idle()
        await asyncio.gather(*sess.tasks)
        return sess

    frames = asyncio.run(go()).said[0]["frames"]
    assert max(f.samples_per_channel for f in frames) <= 16000 // 10


def test_mismatched_chunk_rate_is_resampled_to_the_first_rate(monkeypatch):
    async def go():
        pipe = FakePipeline([("pcm", 0.5, 16000), ("pcm", 0.5, 22050)])
        sess = FakeSession()
        agent = _agent(monkeypatch, pipe, sess)
        await _turn(agent, "mixed")
        await agent.wait_idle()
        await asyncio.gather(*sess.tasks)
        return sess

    frames = asyncio.run(go()).said[0]["frames"]
    assert all(f.sample_rate == 16000 for f in frames)
    total = sum(f.samples_per_channel for f in frames)
    assert total == pytest.approx(16000, abs=400)  # 0.5 s + 0.5 s at 16 kHz


def test_interruption_aborts_the_pipeline_and_unblocks_its_thread(monkeypatch):
    async def go():
        # The session never drains audio, so the pipeline thread blocks on
        # backpressure; interruption must still free it.
        pipe = FakePipeline(block_until_abort=True)
        sess = FakeSession(consume_audio=False)
        agent = _agent(monkeypatch, pipe, sess)
        await _turn(agent, "tell me a long story")
        await asyncio.sleep(0.2)
        sess.said[0]["handle"].finish(interrupted=True)
        await asyncio.wait_for(agent.wait_idle(), 5)
        return pipe

    pipe = asyncio.run(go())
    assert pipe.aborted.is_set()
    assert pipe.running == 0


def test_new_turn_aborts_and_awaits_the_running_one(monkeypatch):
    async def go():
        pipe = FakePipeline(block_until_abort=True)
        sess = FakeSession()
        agent = _agent(monkeypatch, pipe, sess)
        await _turn(agent, "first")
        await asyncio.sleep(0.1)
        pipe.block_until_abort = False
        await _turn(agent, "second")
        await asyncio.wait_for(agent.wait_idle(), 5)
        await asyncio.gather(*sess.tasks)
        return pipe

    pipe = asyncio.run(go())
    assert pipe.turns == ["first", "second"]
    assert pipe.max_running == 1  # never two pipeline turns at once


def test_pipeline_error_ends_the_speech_instead_of_hanging(monkeypatch):
    async def go():
        pipe = FakePipeline(fail="groq down")
        sess = FakeSession()
        agent = _agent(monkeypatch, pipe, sess)
        await _turn(agent, "hello")
        await asyncio.wait_for(agent.wait_idle(), 5)
        await asyncio.wait_for(asyncio.gather(*sess.tasks), 5)
        return pipe

    pipe = asyncio.run(go())
    assert pipe.on_audio_pcm is None


def test_blank_transcript_does_not_run_a_turn(monkeypatch):
    async def go():
        pipe, sess = FakePipeline(), FakeSession()
        agent = _agent(monkeypatch, pipe, sess)
        await _turn(agent, "   ")
        await agent.wait_idle()
        return pipe, sess

    pipe, sess = asyncio.run(go())
    assert pipe.turns == [] and sess.said == []


def test_exit_aborts_a_running_turn(monkeypatch):
    async def go():
        pipe = FakePipeline(block_until_abort=True)
        sess = FakeSession()
        agent = _agent(monkeypatch, pipe, sess)
        await _turn(agent, "go on")
        await asyncio.sleep(0.1)
        t0 = time.monotonic()
        await asyncio.wait_for(agent.on_exit(), 5)
        return pipe, time.monotonic() - t0

    pipe, took = asyncio.run(go())
    assert pipe.aborted.is_set() and pipe.running == 0
    assert took < 5


# ---- async deep_research delivery -------------------------------------------

_JOB = {"job_id": "job-1", "user_text": "current ML inference frameworks", "speak": "vLLM and Triton lead."}


def _research_setup(monkeypatch, *, user="listening", agent="listening"):
    pipe = FakePipeline()
    pipe.finished = [dict(_JOB)]
    pipe.spoken_research = []
    sess = FakeSession()
    sess.user_state, sess.agent_state = user, agent
    return pipe, sess, _agent(monkeypatch, pipe, sess)


def test_finished_research_is_spoken_when_everyone_is_quiet(monkeypatch):
    async def go():
        pipe, sess, agent = _research_setup(monkeypatch)
        assert await agent.deliver_research_once() is True
        await agent.wait_idle()
        await asyncio.gather(*sess.tasks)
        return pipe, sess

    pipe, sess = asyncio.run(go())
    speak, kw = pipe.spoken_research[0]
    assert speak.startswith("About your question on current ML inference frameworks")
    assert kw["tool_name"] == "deep_research" and kw["research_job_id"] == "job-1"
    assert sess.said and sess.said[0]["frames"]  # it was actually played


def test_research_waits_while_the_user_is_speaking(monkeypatch):
    async def go():
        pipe, sess, agent = _research_setup(monkeypatch, user="speaking")
        spoke = await agent.deliver_research_once()
        sess.user_state = "listening"  # user finished: the kept result goes out now
        spoke_later = await agent.deliver_research_once()
        await agent.wait_idle()
        return spoke, spoke_later, pipe

    spoke, spoke_later, pipe = asyncio.run(go())
    assert spoke is False and spoke_later is True
    assert len(pipe.spoken_research) == 1  # kept, not dropped, and said once


def test_nav_guidance_speaks_only_when_the_room_is_quiet(monkeypatch):
    class Feed:
        def __init__(self):
            self.line = "Turn right onto Broadway"

        def guidance_waiting(self):
            return self.line is not None

        def take_guidance(self):
            line, self.line = self.line, None
            return line

    async def go(user):
        pipe = FakePipeline()
        pipe.finished = []
        pipe.spoken_research = []
        feed = Feed()
        pipe.nav_feed = feed
        sess = FakeSession()
        sess.user_state, sess.agent_state = user, "listening"
        agent = _agent(monkeypatch, pipe, sess)
        spoke = await agent.deliver_research_once()
        await agent.wait_idle()
        await asyncio.gather(*sess.tasks)
        return spoke, pipe, feed

    spoke, pipe, feed = asyncio.run(go("speaking"))
    assert spoke is False and pipe.spoken_research == [] and feed.line is not None
    spoke, pipe, feed = asyncio.run(go("listening"))
    assert spoke is True and feed.line is None
    text, kw = pipe.spoken_research[0]
    assert text == "Turn right onto Broadway" and kw["tool_name"] == "navigation"


def test_finished_research_is_spoken_ahead_of_nav_guidance(monkeypatch):
    class Feed:
        def __init__(self):
            self.line = "Turn right"

        def guidance_waiting(self):
            return True

        def take_guidance(self):
            raise AssertionError("guidance must wait behind a finished answer")

    async def go():
        pipe, sess, agent = _research_setup(monkeypatch)
        pipe.nav_feed = Feed()
        assert await agent.deliver_research_once() is True
        await agent.wait_idle()
        return pipe

    pipe = asyncio.run(go())
    assert pipe.spoken_research[0][1]["tool_name"] == "deep_research"


def test_research_waits_while_a_turn_is_running(monkeypatch):
    async def go():
        pipe, sess, agent = _research_setup(monkeypatch)
        pipe.block_until_abort = True
        await _turn(agent, "tell me a long story")
        await asyncio.sleep(0.05)
        spoke = await agent.deliver_research_once()
        await agent.on_exit()
        return spoke, pipe

    spoke, pipe = asyncio.run(go())
    assert spoke is False and pipe.spoken_research == []


class RecordingMirror:
    def __init__(self):
        self.calls = []
        self.generation_id = 0

    def turn_started(self, text):
        self.generation_id += 1
        self.calls.append(("start", text, self.generation_id))
        return self.generation_id

    def trace(self, gen, payload):
        self.calls.append(("trace", gen, payload.get("router")))

    def turn_finished(self, gen, transcript, reply, tool_name):
        self.calls.append(("finish", gen, transcript, reply, tool_name))

    def event(self, msg):
        self.calls.append(("event", msg.get("type")))


class TracingPipeline(FakePipeline):
    on_agent_trace = None

    def run_text_turn(self, text):
        self.on_agent_trace({"router": "regex", "calls": []})
        out = super().run_text_turn(text)
        out = _Result()
        out.assistant_text, out.tool_name = "Driver side set to 23 degrees.", "vehicle"
        return out


def test_a_turn_is_mirrored_for_displays(monkeypatch):
    async def go():
        pipe, sess, mirror = TracingPipeline(), FakeSession(), RecordingMirror()
        monkeypatch.setattr(na.NovaAgent, "session", property(lambda self: sess))
        agent = na.NovaAgent(pipe, on_event=lambda m: None, observe=mirror)
        await _turn(agent, "set the temperature to 23")
        await agent.wait_idle()
        await asyncio.gather(*sess.tasks)
        return pipe, mirror

    pipe, mirror = asyncio.run(go())
    assert mirror.calls[0] == ("start", "set the temperature to 23", 1)
    assert ("trace", 1, "regex") in mirror.calls
    assert ("finish", 1, "set the temperature to 23", "Driver side set to 23 degrees.",
            "vehicle") in mirror.calls
    assert ("event", "nova.tool_status") in mirror.calls      # what the caller gets, displays get
    assert pipe.on_agent_trace is None                         # the hook ends with the turn
