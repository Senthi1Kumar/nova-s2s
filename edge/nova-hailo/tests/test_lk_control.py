"""NovaControl: nova.* JSON over the LiveKit data channel, same vocabulary as /v1/realtime."""
import asyncio
import json

import pytest

pytest.importorskip("livekit.rtc")

from nova_hailo.lk import control  # noqa: E402


class FakeParticipant:
    def __init__(self):
        self.sent: list[dict] = []

    async def publish_data(self, payload, *, reliable=True, topic=""):
        assert reliable and topic == control.TOPIC
        self.sent.append(json.loads(payload))


class FakeRoom:
    def __init__(self):
        self.local_participant = FakeParticipant()
        self.handlers = {}

    def on(self, event, cb):
        self.handlers[event] = cb


class Packet:
    def __init__(self, msg, topic=control.TOPIC):
        self.data = msg if isinstance(msg, bytes) else json.dumps(msg).encode()
        self.topic = topic


class Pipe:
    def __init__(self):
        self.mode = "local"

    def llm_settings_snapshot(self):
        return {"mode": self.mode}

    def apply_llm_settings(self, *, mode=None, local_hef=None, or_model=None):
        self.mode = mode
        return {"mode": mode}


@pytest.fixture(autouse=True)
def _no_integrations(monkeypatch):
    monkeypatch.setattr(control.settings_ops, "integrations_snapshot", lambda: {"google": {}})


async def _deliver(ctl, room, msg, topic=control.TOPIC):
    room.handlers["data_received"](Packet(msg, topic))
    await ctl.drain()


def _types(room):
    return [m["type"] for m in room.local_participant.sent]


def test_settings_get_replies_with_llm_and_integrations():
    async def go():
        room = FakeRoom()
        ctl = control.NovaControl(room, Pipe())
        ctl.attach()
        await _deliver(ctl, room, {"type": "nova.settings.get"})
        return room

    sent = asyncio.run(go()).local_participant.sent
    assert sent == [{"type": "nova.settings", "mode": "local", "google": {}, "transport": "livekit"}]


def test_llm_switch_reports_loading_then_settings_then_ready():
    async def go():
        room, pipe = FakeRoom(), Pipe()
        ctl = control.NovaControl(room, pipe)
        ctl.attach()
        await _deliver(ctl, room, {"type": "nova.settings.set", "settings": {"mode": "openrouter"}})
        return room, pipe

    room, pipe = asyncio.run(go())
    assert pipe.mode == "openrouter"
    assert _types(room) == ["nova.llm_status", "nova.settings", "nova.llm_status"]
    assert room.local_participant.sent[0]["status"] == "loading"
    assert room.local_participant.sent[1]["mode"] == "openrouter"
    assert room.local_participant.sent[2]["status"] == "ready"


def test_other_topics_and_bad_json_are_ignored_or_rejected():
    async def go():
        room = FakeRoom()
        ctl = control.NovaControl(room, Pipe())
        ctl.attach()
        await _deliver(ctl, room, {"type": "nova.settings.get"}, topic="lk.chat")
        await _deliver(ctl, room, b"{not json")
        await _deliver(ctl, room, {"type": "nope"})
        return room

    sent = asyncio.run(go()).local_participant.sent
    assert [m["error"]["type"] for m in sent] == ["invalid_json", "unknown_message"]


def test_oversized_messages_are_rejected():
    async def go():
        room = FakeRoom()
        ctl = control.NovaControl(room, Pipe())
        ctl.attach()
        await _deliver(ctl, room, {"type": "nova.settings.get", "pad": "x" * 20000})
        return room

    sent = asyncio.run(go()).local_participant.sent
    assert sent[0]["error"]["type"] == "message_too_large"


def test_send_publishes_agent_events():
    async def go():
        room = FakeRoom()
        ctl = control.NovaControl(room, Pipe())
        ctl.send({"type": "nova.turn_metrics", "ttfa_ms": 900})
        await ctl.drain()
        return room

    assert asyncio.run(go()).local_participant.sent == [{"type": "nova.turn_metrics", "ttfa_ms": 900}]


def test_tap_to_wake_calls_the_wake_hook():
    async def go():
        room = FakeRoom()
        woke = []
        ctl = control.NovaControl(room, Pipe())
        ctl.on_wake = lambda: woke.append("tap")
        ctl.attach()
        await _deliver(ctl, room, {"type": "nova.wake"})
        return woke, room

    woke, room = asyncio.run(go())
    assert woke == ["tap"] and room.local_participant.sent == []


def test_settings_carry_the_wake_state():
    async def go():
        room = FakeRoom()
        ctl = control.NovaControl(room, Pipe())
        ctl.state_extra = lambda: {"wake_state": "idle", "wake_mode": "kws"}
        ctl.attach()
        await _deliver(ctl, room, {"type": "nova.settings.get"})
        return room

    sent = asyncio.run(go()).local_participant.sent[0]
    assert sent["wake_state"] == "idle" and sent["wake_mode"] == "kws"


def test_an_llm_switch_from_the_browser_is_announced_for_displays():
    async def go():
        room, pipe, seen = FakeRoom(), Pipe(), []
        ctl = control.NovaControl(room, pipe)
        ctl.on_llm_changed = lambda: seen.append("changed")
        ctl.attach()
        await _deliver(ctl, room, {"type": "nova.settings.set", "settings": {"mode": "openrouter"}})
        await _deliver(ctl, room, {"type": "nova.settings.get"})
        return seen

    assert asyncio.run(go()) == ["changed"]        # the switch, not the read


def test_the_browser_can_mute_the_cabin_mic():
    async def go():
        room, seen = FakeRoom(), []
        ctl = control.NovaControl(room, Pipe())
        ctl.on_mic_set = seen.append
        ctl.attach()
        await _deliver(ctl, room, {"type": "nova.mic.set", "muted": True})
        await _deliver(ctl, room, {"type": "nova.mic.set", "muted": "yes"})
        return room, seen

    room, seen = asyncio.run(go())
    assert seen == [True]
    assert room.local_participant.sent[-1]["error"]["type"] == "bad_muted"
