"""LiveKit-door sessions mirrored to /v1/observe with the realtime door's event shapes."""
import asyncio
import threading

from nova_hailo.lk.observe_mirror import ObserveMirror
from nova_hailo.web.observe import ObserverHub


class FakeHub:
    def __init__(self):
        self.events = []

    def publish_session_event_threadsafe(self, event):
        self.events.append(event)

    def session_started_threadsafe(self):
        self.events.append({"type": "observe.session", "active": True})

    def session_ended_threadsafe(self):
        self.events.append({"type": "observe.session", "active": False})


def states(hub):
    return [e["fsm"]["state"] for e in hub.events if e["type"] == "nova.fsm"]


def test_a_spoken_turn_reads_like_a_realtime_turn():
    hub = FakeHub()
    m = ObserveMirror(hub)
    m.started()
    m.on_gate("listening")
    m.on_user_state("speaking")
    gen = m.turn_started("set the temperature to 23")
    m.on_user_state("listening")
    m.on_agent_state("speaking")
    m.turn_finished(gen, "set the temperature to 23", "Driver side set to 23 degrees.", None)
    m.on_agent_state("listening")
    m.ended()
    types = [e["type"] for e in hub.events]
    assert types[0] == "observe.session" and hub.events[0]["active"] is True
    assert hub.events[-1] == {"type": "observe.session", "active": False}
    assert states(hub) == ["LISTENING", "THINKING", "SPEAKING", "LISTENING", "IDLE"]
    user = next(e for e in hub.events
                if e["type"] == "conversation.item.input_audio_transcription.completed")
    reply = next(e for e in hub.events if e["type"] == "response.audio_transcript.done")
    assert user == {"type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "set the temperature to 23", "generation_id": gen}
    assert reply == {"type": "response.audio_transcript.done",
                     "transcript": "Driver side set to 23 degrees.", "generation_id": gen}
    fsm = next(e for e in hub.events if e["type"] == "nova.fsm")
    assert fsm["session_id"] == "livekit" and "generation_id" in fsm["fsm"]


def test_a_closed_wake_gate_is_idle_and_a_wake_opens_it():
    hub = FakeHub()
    m = ObserveMirror(hub)
    m.started()
    m.on_gate("idle")
    m.on_gate("listening")
    assert states(hub) == ["LISTENING", "IDLE", "LISTENING"]


def test_trace_and_card_carry_the_turn_generation():
    hub = FakeHub()
    m = ObserveMirror(hub)
    m.started()
    gen = m.turn_started("take me to work")
    trace = {"router": "regex", "calls": [{"device": "navigation", "api": "navigate_to",
                                            "args": {"destination": "work"}}],
             "gate": ["allow"], "diff": [], "speak": "Navigating to work."}
    m.trace(gen, trace)
    m.turn_finished(gen, "take me to work", "Navigating to work.", "vehicle")
    tr = next(e for e in hub.events if e["type"] == "nova.agent_trace")
    assert tr["generation_id"] == gen and tr["router"] == "regex"
    card = next(e for e in hub.events if e["type"] == "nova.card")
    assert card["generation_id"] == gen and card["kind"] == "route" and card["title"] == "work"


def test_a_stale_turn_does_not_publish_after_a_newer_one_started():
    hub = FakeHub()
    m = ObserveMirror(hub)
    m.started()
    old = m.turn_started("first")
    new = m.turn_started("second")
    m.trace(old, {"router": "regex"})
    m.turn_finished(old, "first", "Old answer.", None)
    assert not [e for e in hub.events if e.get("generation_id") == old
                and e["type"] in ("nova.agent_trace", "response.audio_transcript.done")]
    m.turn_finished(new, "second", "New answer.", None)
    assert states(hub)[-1] == "LISTENING"


def test_other_agent_events_pass_through():
    hub = FakeHub()
    m = ObserveMirror(hub)
    m.event({"type": "nova.card", "generation_id": None, "kind": "answer", "title": "Done",
             "lines": ["Done."], "items": []})
    assert hub.events[-1]["title"] == "Done"


def test_the_hub_accepts_events_from_another_threads_loop():
    hub = ObserverHub()

    async def go():
        hub.bind(asyncio.get_running_loop())
        q = hub.subscribe()

        def job_thread():
            hub.session_started_threadsafe()
            hub.publish_session_event_threadsafe({"type": "nova.fsm", "fsm": {"state": "LISTENING"}})
            hub.session_ended_threadsafe()

        t = threading.Thread(target=job_thread)
        t.start()
        t.join()
        return [await asyncio.wait_for(q.get(), 1) for _ in range(3)]

    got = asyncio.run(go())
    assert got[0] == {"type": "observe.session", "active": True}
    assert got[1]["type"] == "nova.fsm" and got[2]["active"] is False
    assert hub.replay()[0]["fsm"]["state"] == "LISTENING"
    assert hub.session_active is False


def test_an_unbound_hub_drops_threadsafe_events():
    hub = ObserverHub()
    hub.publish_session_event_threadsafe({"type": "nova.fsm", "fsm": {"state": "IDLE"}})
    hub.session_started_threadsafe()
    assert hub.replay() == [] and hub.session_active is False
