"""/v1/observe: a read-only mirror of the session's events for displays (no audio, no lease)."""
import asyncio
import json

from nova_hailo.web.observe import ObserverHub, serve_observer


def test_session_events_are_mirrored_and_audio_is_not():
    hub = ObserverHub()
    q = hub.subscribe()
    hub.publish_session_event({"type": "nova.agent_trace", "router": "regex"})
    hub.publish_session_event({"type": "response.audio.delta", "delta": "AAAA"})
    hub.publish_session_event({"type": "response.audio_transcript.delta", "delta": "Hi"})
    hub.publish_session_event({"type": "session.created", "session": {}})
    got = [q.get_nowait()["type"] for _ in range(q.qsize())]
    assert got == ["nova.agent_trace", "response.audio_transcript.delta"]


def test_vehicle_state_comes_only_from_the_app_feed():
    hub = ObserverHub()
    q = hub.subscribe()
    hub.publish_session_event({"type": "nova.vehicle", "seq": 3, "changed": {}})  # per-session copy
    hub.publish_vehicle({"type": "nova.vehicle", "seq": 1, "changed": {"Vehicle.Speed": 10}})
    assert q.qsize() == 1 and q.get_nowait()["seq"] == 1


def test_a_full_queue_drops_the_oldest_and_never_blocks():
    hub = ObserverHub(maxsize=2)
    q = hub.subscribe()
    for i in range(5):
        hub.publish_session_event({"type": "nova.tool_status", "i": i})
    assert [q.get_nowait()["i"] for _ in range(q.qsize())] == [3, 4]


class FakeWS:
    def __init__(self, fail_after=None):
        self.sent = []
        self.fail_after = fail_after

    async def send_text(self, text):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RuntimeError("client went away")
        self.sent.append(json.loads(text))


def test_a_late_observer_gets_hello_fsm_snapshot_and_last_trace_then_live_events():
    async def go():
        hub = ObserverHub()
        hub.session_started()
        hub.publish_session_event({"type": "nova.fsm", "fsm": {"state": "listening"}})
        hub.publish_session_event({"type": "nova.agent_trace", "router": "regex", "speak": "old"})
        hub.publish_vehicle({"type": "nova.vehicle", "seq": 7, "changed": {"Vehicle.Speed": 3}})
        ws = FakeWS()
        task = asyncio.create_task(serve_observer(
            ws, hub, snapshot=lambda seq: {"type": "nova.vehicle", "seq": seq, "snapshot": {}}))
        await asyncio.sleep(0)
        hub.publish_session_event({"type": "nova.agent_trace", "router": "regex", "speak": "new"})
        await asyncio.sleep(0.01)
        hub.close()
        await asyncio.wait_for(task, 1.0)
        return ws, hub

    ws, hub = asyncio.run(go())
    types = [e["type"] for e in ws.sent]
    assert types == ["observe.hello", "nova.fsm", "nova.vehicle", "nova.agent_trace", "nova.agent_trace"]
    assert ws.sent[0] == {"type": "observe.hello", "protocol": "nova-observe/1", "session_active": True}
    assert ws.sent[2]["seq"] == 7 and "snapshot" in ws.sent[2]  # current seq, not 0
    assert ws.sent[3]["speak"] == "old" and ws.sent[4]["speak"] == "new"
    assert hub.observer_count() == 0


def test_session_start_and_end_are_announced():
    hub = ObserverHub()
    q = hub.subscribe()
    assert hub.session_active is False
    hub.session_started()
    assert hub.session_active is True
    hub.session_ended()
    assert [q.get_nowait() for _ in range(2)] == [
        {"type": "observe.session", "active": True}, {"type": "observe.session", "active": False}]


def test_wake_detected_is_mirrored():
    hub = ObserverHub()
    q = hub.subscribe()
    hub.publish_session_event({"type": "wake.detected", "source": "kws"})
    assert q.get_nowait()["type"] == "wake.detected"


def test_a_disconnected_observer_is_removed():
    async def go():
        hub = ObserverHub()
        ws = FakeWS(fail_after=1)  # ready goes out, the next send fails
        task = asyncio.create_task(serve_observer(ws, hub, snapshot=lambda seq: None))
        await asyncio.sleep(0)
        hub.publish_session_event({"type": "nova.state", "state": "listening"})
        await asyncio.wait_for(task, 1.0)
        return hub

    assert asyncio.run(go()).observer_count() == 0


def test_publish_from_a_worker_thread_hops_onto_the_loop():
    import threading

    async def go():
        hub = ObserverHub()
        hub.bind(asyncio.get_running_loop())
        q = hub.subscribe()
        t = threading.Thread(target=hub.publish_vehicle_threadsafe,
                             args=({"type": "nova.vehicle", "seq": 1, "changed": {}},))
        t.start()
        t.join()
        return await asyncio.wait_for(q.get(), 1.0)

    assert asyncio.run(go())["seq"] == 1


def test_a_late_observer_also_gets_the_latest_nav_after_the_snapshot():
    async def go():
        hub = ObserverHub()
        hub.publish_vehicle({"type": "nova.nav", "active": True, "route": "abc"})
        ws = FakeWS()
        task = asyncio.create_task(serve_observer(
            ws, hub, snapshot=lambda seq: {"type": "nova.vehicle", "seq": seq, "snapshot": {}}))
        await asyncio.sleep(0.01)
        hub.close()
        await asyncio.wait_for(task, 1.0)
        return ws

    types = [e["type"] for e in asyncio.run(go()).sent]
    assert types == ["observe.hello", "nova.vehicle", "nova.nav"]


def test_duck_is_mirrored():
    from nova_hailo.web.observe import _mirrored

    assert _mirrored("output_audio.duck")


def test_interrupt_is_mirrored():
    from nova_hailo.web.observe import _mirrored

    assert _mirrored("playback.interrupted")


def test_interrupt_handler_publishes(monkeypatch):
    from nova_hailo.web import realtime_session as rs

    seen = []
    monkeypatch.setattr(rs.OBSERVERS, "publish_session_event", seen.append)
    rs.publish_playback_interrupted({"type": "playback.interrupted", "reason": "barge_in"})
    rs.publish_playback_interrupted({"type": "playback.interrupted"})
    assert seen == [
        {"type": "playback.interrupted", "reason": "barge_in"},
        {"type": "playback.interrupted", "reason": "stop"},
    ]
