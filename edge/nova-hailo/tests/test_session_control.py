"""Process-wide mic mute and session control (/v1/session, nova.mic)."""
from nova_hailo.web.observe import ObserverHub
from nova_hailo.web.session_control import SessionControl


class Hub:
    def __init__(self):
        self.events = []

    def publish_session_event_threadsafe(self, event):
        self.events.append(event)


class Handle:
    def __init__(self, state="LISTENING"):
        self.state, self.calls = state, []

    def control_mute(self, muted):
        self.calls.append(("mute", muted))

    def control_arm(self):
        self.calls.append(("arm",))

    def control_cancel(self):
        self.calls.append(("cancel",))

    def control_state(self):
        return self.state


def test_mute_publishes_nova_mic_only_on_change():
    hub, h = Hub(), Handle()
    ctl = SessionControl(hub)
    ctl.register(h)
    assert ctl.set_muted(True, "cockpit") is True
    assert ctl.set_muted(True, "cockpit") is False          # unchanged: no frame, no call
    assert hub.events == [{"type": "nova.mic", "muted": True, "source": "cockpit"}]
    assert h.calls == [("mute", True)]
    ctl.set_muted(False, "realtime")
    assert hub.events[-1] == {"type": "nova.mic", "muted": False, "source": "realtime"}


def test_mute_outlives_a_session_and_reaches_the_next_one():
    hub = Hub()
    ctl = SessionControl(hub)
    unregister = ctl.register(Handle())
    ctl.set_muted(True, "cockpit")
    unregister()
    nxt = Handle()
    ctl.register(nxt)
    assert ctl.muted is True and nxt.calls == [("mute", True)]   # told at registration


def test_arm_is_refused_while_muted_or_without_a_session():
    ctl = SessionControl(Hub())
    assert ctl.arm() == {"ok": False, "error": "no_session"}
    h = Handle()
    ctl.register(h)
    ctl.set_muted(True, "cockpit")
    assert ctl.arm() == {"ok": False, "error": "mic_muted"}
    ctl.set_muted(False, "cockpit")
    assert ctl.arm() == {"ok": True} and ("arm",) in h.calls


def test_cancel_reaches_the_session_and_is_always_ok():
    ctl = SessionControl(Hub())
    assert ctl.cancel() == {"ok": True}
    h = Handle()
    ctl.register(h)
    assert ctl.cancel() == {"ok": True} and h.calls[-1] == ("cancel",)


def test_status_reports_mute_source_and_the_session_state():
    ctl = SessionControl(Hub())
    assert ctl.status() == {"muted": False, "source": None, "state": "IDLE"}
    ctl.register(Handle("SPEAKING"))
    ctl.set_muted(True, "cockpit")
    assert ctl.status() == {"muted": True, "source": "cockpit", "state": "SPEAKING"}


def test_the_latest_mic_state_is_replayed_to_a_new_observer():
    hub = ObserverHub()
    hub.publish_session_event({"type": "nova.mic", "muted": True, "source": "cockpit"})
    assert {"type": "nova.mic", "muted": True, "source": "cockpit"} in hub.replay()
