"""WakeGate: idle until the wake word, then a short follow-up window of silence."""
from nova_hailo.lk.wake_gate import WakeGate


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _gate(followup=5.0):
    clock = Clock()
    return WakeGate(followup_s=followup, clock=clock), clock


def test_starts_idle_and_wakes_once():
    gate, _ = _gate()
    assert gate.state == "idle"
    assert gate.wake("kws") is True
    assert gate.state == "listening"
    assert gate.wake("kws") is False  # already awake: no second chime


def test_silence_after_waking_closes_the_window():
    gate, clock = _gate(5.0)
    gate.wake("kws")
    clock.t += 4.9
    assert gate.expire() is False
    clock.t += 0.2
    assert gate.expire() is True and gate.state == "idle"


def test_window_never_closes_while_someone_is_busy():
    gate, clock = _gate(5.0)
    gate.wake("kws")
    gate.busy(True)  # user speaking / Nova thinking or speaking
    clock.t += 60
    assert gate.expire() is False and gate.state == "listening"


def test_follow_up_window_restarts_when_nova_finishes():
    gate, clock = _gate(5.0)
    gate.wake("kws")
    gate.busy(True)
    clock.t += 30  # a long answer
    gate.busy(False)  # reply done: the 5 s of silence starts now
    clock.t += 4.0
    assert gate.expire() is False
    clock.t += 1.5
    assert gate.expire() is True


def test_activity_while_idle_does_not_open_the_gate():
    gate, clock = _gate()
    gate.busy(True)
    gate.busy(False)  # e.g. a research result spoken while idle
    assert gate.state == "idle"
    clock.t += 100
    assert gate.expire() is False  # nothing to expire


def test_always_open_mode_never_expires():
    gate, clock = _gate()
    gate.open_forever()
    clock.t += 1e6
    assert gate.expire() is False and gate.state == "listening"


# ---- WakeController: gate <-> LiveKit session/room --------------------------

import asyncio  # noqa: E402

from nova_hailo.lk.wake_gate import WakeController  # noqa: E402


class FakeDetector:
    def __init__(self, hit_on: int):
        self.hit_on, self.calls = hit_on, 0

    def process(self, pcm):
        self.calls += 1
        return self.calls == self.hit_on


class Frame:
    data = b"\x00\x00" * 1280


async def _frames(n):
    for _ in range(n):
        yield Frame()
        await asyncio.sleep(0)


def _controller(detector=None, followup=5.0):
    clock = Clock()
    audio, states = [], []
    ctl = WakeController(
        gate=WakeGate(followup_s=followup, clock=clock),
        detector=detector,
        set_audio_enabled=audio.append,
        send_state=lambda state, **kw: states.append({"state": state, **kw}),
    )
    return ctl, clock, audio, states


def test_starts_idle_with_speech_input_paused():
    ctl, _, audio, states = _controller(FakeDetector(hit_on=99))
    ctl.start()
    assert audio == [False] and states == [{"state": "idle"}]


def test_wake_word_opens_input_and_announces_listening_once():
    async def go():
        det = FakeDetector(hit_on=3)
        ctl, _, audio, states = _controller(det)
        ctl.start()
        await ctl.run_kws(_frames(10))
        return det, audio, states

    det, audio, states = asyncio.run(go())
    assert audio == [False, True]
    assert states[-1] == {"state": "listening", "wake": "kws"}
    assert det.calls == 3  # the detector rests while the gate is open


def test_silence_closes_the_window_and_pauses_input_again():
    ctl, clock, audio, states = _controller(FakeDetector(hit_on=99), followup=5.0)
    ctl.start()
    ctl.wake("tap")
    ctl.on_agent_state("speaking")
    clock.t += 20
    assert ctl.check_expiry() is False  # Nova still talking
    ctl.on_agent_state("listening")
    clock.t += 5.1
    assert ctl.check_expiry() is True
    assert audio[-1] is False and states[-1] == {"state": "idle"}


def test_user_speech_holds_the_window_open():
    ctl, clock, _, _ = _controller(FakeDetector(hit_on=99))
    ctl.start()
    ctl.wake("kws")
    ctl.on_user_state("speaking")
    clock.t += 30
    assert ctl.check_expiry() is False


def test_without_a_detector_it_is_always_listening():
    ctl, clock, audio, states = _controller(detector=None)
    ctl.start()
    clock.t += 1e6
    assert ctl.check_expiry() is False
    assert audio == [] and states == [{"state": "listening", "wake": "session"}]


def test_mute_pauses_speech_input_and_unmute_restores_it():
    ctl, _, audio, _ = _controller(detector=None)
    ctl.start()
    ctl.set_muted(True)
    ctl.set_muted(False)
    assert audio == [False, True]


def test_mute_beats_a_wake_and_the_wake_word():
    async def go():
        ctl, _, audio, states = _controller(FakeDetector(hit_on=1))
        ctl.start()
        ctl.set_muted(True)
        ctl.wake("tap")
        await ctl.run_kws(_frames(3))
        return ctl, audio, states

    ctl, audio, states = asyncio.run(go())
    assert ctl.gate.state == "idle" and True not in audio
    assert states == [{"state": "idle"}]
    ctl.set_muted(False)
    assert audio[-1] is False            # unmuted, but the gate is still closed


def test_close_shuts_an_open_gate():
    ctl, _, audio, states = _controller(FakeDetector(hit_on=99))
    ctl.start()
    ctl.wake("tap")
    ctl.close()
    assert ctl.gate.state == "idle" and audio[-1] is False and states[-1] == {"state": "idle"}
