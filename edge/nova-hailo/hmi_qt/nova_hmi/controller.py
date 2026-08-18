"""QML-facing controller. Same properties as the NovaController."""
from __future__ import annotations

from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot

from nova_hmi.protocol import PHASES, map_fsm_to_phase

DEMO_SCRIPT = [
    ("idle", 1400, None, None),
    ("listening", 2600, "user", "How much range have I got left?"),
    ("thinking", 1500, None, None),
    (
        "speaking",
        3200,
        "nova",
        "About 180 kilometres at your current draw. That covers the drive home.",
    ),
    ("idle", 2000, None, None),
]


class NovaController(QObject):
    phaseChanged = Signal(str)
    levelChanged = Signal(float)
    bandsChanged = Signal()
    linkChanged = Signal(str)
    latencyChanged = Signal(int)
    backendLabelChanged = Signal(str)
    transcriptAdded = Signal(str, str)
    cleared = Signal()

    def __init__(self, audio, bridge=None, parent=None):
        super().__init__(parent)
        self._phase = "idle"
        self._level = 0.0
        self._bands = [0.0] * 24
        self._link = "demo"
        self._latency = 0
        self._backend_label = "connecting…" if bridge is not None else "demo script"
        self._asst = ""
        self._last_nova = ""
        self._audio = audio
        self._bridge = bridge
        self._live = bridge is not None
        audio.frame.connect(self._on_frame)
        if hasattr(audio, "pcm16Ready") and bridge is not None:
            audio.pcm16Ready.connect(bridge.send_pcm16)
        if hasattr(audio, "playbackStarted") and bridge is not None:
            audio.playbackStarted.connect(lambda: bridge.playback_started())
        if hasattr(audio, "playbackQueueEmpty"):
            audio.playbackQueueEmpty.connect(self._on_play_empty)

        if bridge is not None:
            bridge.connected.connect(self._on_link_up)
            bridge.disconnected.connect(self._on_link_down)
            bridge.phaseReceived.connect(self._set_phase_from_server)
            bridge.userTranscript.connect(self._on_user)
            bridge.assistantDelta.connect(self._on_asst_delta)
            bridge.assistantDone.connect(self._on_asst_done)
            bridge.audioDelta.connect(self._on_audio)
            bridge.playbackCancel.connect(self._on_cancel)
            bridge.toolStatus.connect(self._set_label)
            bridge.greeting.connect(lambda t: self.transcriptAdded.emit("nova", t))
            bridge.latencyMs.connect(self._set_latency)
            bridge.turnDone.connect(self._on_turn_done)
            if hasattr(bridge, "failClosed"):
                bridge.failClosed.connect(self._on_fail_closed)

        self._speak_watch = QTimer(self)
        self._speak_watch.setSingleShot(True)
        self._speak_watch.setInterval(2200)
        self._speak_watch.timeout.connect(self._force_listen)

        self._step = 0
        self._demo = QTimer(self)
        self._demo.setSingleShot(True)
        self._demo.timeout.connect(self._advance_demo)
        if not self._live:
            self._demo.start(600)

    @Property(str, notify=phaseChanged)
    def phase(self) -> str:
        return self._phase

    @Property(float, notify=levelChanged)
    def level(self) -> float:
        return self._level

    @Property("QVariantList", notify=bandsChanged)
    def bands(self) -> list:
        return self._bands

    @Property(str, notify=linkChanged)
    def link(self) -> str:
        return self._link

    @Property(int, notify=latencyChanged)
    def latencyMs(self) -> int:
        return self._latency

    @Property(str, notify=backendLabelChanged)
    def backendLabel(self) -> str:
        return self._backend_label

    @Slot(str)
    def setPhase(self, value: str) -> None:
        """Bench buttons / keys. Live backend owns phase; this is visual-only."""
        if value not in PHASES or value == self._phase:
            return
        self._phase = value
        self.phaseChanged.emit(value)

    def _set_phase_from_server(self, value: str) -> None:
        phase = value if value in PHASES else map_fsm_to_phase(value)
        if phase == self._phase:
            return
        # Stay SPEAKING only while local TTS is actually queued.
        if (
            self._phase == "speaking"
            and phase not in {"speaking", "idle"}
            and hasattr(self._audio, "play_queue_depth")
            and self._audio.play_queue_depth() > 0
        ):
            return
        self._phase = phase
        self.phaseChanged.emit(phase)

    @Slot()
    def clearTranscript(self) -> None:
        self.cleared.emit()

    def _on_frame(self, level: float, bands: list) -> None:
        a = 0.55 if level > self._level else 0.12
        new = self._level + (level - self._level) * a
        if abs(new - self._level) > 0.002:
            self._level = new
            self.levelChanged.emit(new)
        self._bands = bands
        self.bandsChanged.emit()

    def _set_latency(self, ms: int) -> None:
        self._latency = int(ms)
        self.latencyChanged.emit(self._latency)

    def _set_label(self, text: str) -> None:
        self._backend_label = text
        self.backendLabelChanged.emit(text)

    def _on_user(self, text: str) -> None:
        t = (text or "").strip()
        if not t:
            return
        if self._is_echo(t):
            print(f"[hmi] drop echo transcript: {t!r}", flush=True)
            return
        self.transcriptAdded.emit("user", t)

    def _is_echo(self, user: str) -> bool:
        prev = (self._last_nova or "").lower()
        u = user.lower()
        if not prev or len(u) < 4:
            return False
        pw = {w for w in prev.replace("'", "").split() if len(w) > 2}
        uw = {w for w in u.replace("'", "").split() if len(w) > 2}
        if not uw:
            return False
        overlap = len(pw & uw) / max(1, len(uw))
        return overlap >= 0.5 or u in prev or prev[:24] in u

    def _on_asst_delta(self, piece: str) -> None:
        self._asst += piece

    def _on_asst_done(self, text: str) -> None:
        final = (text or self._asst).strip()
        self._asst = ""
        if final:
            self._last_nova = final
            self.transcriptAdded.emit("nova", final)

    def _force_listen(self) -> None:
        self._speak_watch.stop()
        if hasattr(self._audio, "flush_playback"):
            # Drop any stuck tail so the UI cannot freeze on SPEAKING.
            if hasattr(self._audio, "play_queue_depth") and self._audio.play_queue_depth() == 0:
                pass
        self._set_phase_from_server("listening")

    def _on_audio(self, pcm: bytes, rate: int) -> None:
        if hasattr(self._audio, "enqueue_playback"):
            self._audio.enqueue_playback(pcm, rate)
        self._set_phase_from_server("speaking")
        self._speak_watch.start(2200)

    def _on_cancel(self) -> None:
        if hasattr(self._audio, "flush_playback"):
            self._audio.flush_playback()
        if self._bridge is not None:
            self._bridge.playback_interrupted()
        self._force_listen()

    def _on_fail_closed(self, reason: str) -> None:
        self._set_label(f"didn't catch ({reason})")
        self._speak_watch.start(1200)

    def _on_play_empty(self) -> None:
        self._force_listen()

    def _on_turn_done(self) -> None:
        if hasattr(self._audio, "play_queue_depth") and self._audio.play_queue_depth() > 0:
            self._speak_watch.start(1500)
            return
        self._force_listen()

    def _on_link_up(self) -> None:
        self._demo.stop()
        self._link = "online"
        self.linkChanged.emit(self._link)
        mic = "mic live" if not self._audio.synthetic else "NO MIC uplink"
        self._set_label(f"nova-hailo · {mic}")

    def _on_link_down(self) -> None:
        self._link = "offline"
        self.linkChanged.emit(self._link)
        err = ""
        if self._bridge is not None:
            err = str(getattr(self._bridge, "last_error", "") or "")
        self._set_label(err or "reconnecting…")
        if not self._live:
            self._link = "demo"
            self.linkChanged.emit(self._link)
            self._set_label("demo script")
            if not self._demo.isActive():
                self._demo.start(800)

    def _advance_demo(self) -> None:
        if self._live:
            return
        phase, dwell, role, text = DEMO_SCRIPT[self._step % len(DEMO_SCRIPT)]
        self.setPhase(phase)
        if role and text:
            self.transcriptAdded.emit(role, text)
        if phase == "speaking":
            self._set_latency(380)
        self._step += 1
        self._demo.start(dwell)
