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
    llmModeChanged = Signal(str)
    localHefChanged = Signal(str)
    orModelChanged = Signal(str)
    llmStatusChanged = Signal(str)
    hasOrKeyChanged = Signal(bool)
    settingsOpenChanged = Signal(bool)
    localModelsChanged = Signal()
    orModelsChanged = Signal()
    chatOpenChanged = Signal(bool)
    opsOpenChanged = Signal(bool)
    liveUserChanged = Signal(str)
    liveAssistantChanged = Signal(str)
    liveVisibleChanged = Signal(bool)
    toolCapsuleChanged = Signal(str)
    toolCapsuleStatusChanged = Signal(str)
    sttMsChanged = Signal(float)
    llmMsChanged = Signal(float)
    llmTtftMsChanged = Signal(float)
    llmDecodeMsChanged = Signal(float)
    ttsMsChanged = Signal(float)
    ttfaMsChanged = Signal(float)
    e2eMsChanged = Signal(float)
    sttPathChanged = Signal(str)
    nsStrengthChanged = Signal(float)
    gateRmsChanged = Signal(float)
    nsOnChanged = Signal(bool)

    def __init__(self, audio, bridge=None, parent=None):
        super().__init__(parent)
        self._phase = "idle"
        self._level = 0.0
        self._bands = [0.0] * 24
        self._link = "demo"
        self._latency = 0
        self._backend_label = "connecting…" if bridge is not None else "demo script"
        self._llm_mode = "local"
        self._local_hef = "qwen2"
        self._or_model = "deepseek/deepseek-v4-flash-0731"
        self._llm_status = ""
        self._has_or_key = False
        self._settings_open = False
        self._local_models = [
            {"id": "qwen2", "label": "Qwen2 1.5B"},
            {"id": "qwen25", "label": "Qwen2.5 1.5B"},
            {"id": "qwen3", "label": "Qwen3 1.7B"},
        ]
        self._or_models = [
            {"id": "deepseek/deepseek-v4-flash-0731", "label": "DeepSeek V4 Flash"},
            {"id": "deepseek/deepseek-v3.2", "label": "DeepSeek V3.2"},
            {"id": "qwen/qwen3.8-flash", "label": "Qwen3.8 Flash"},
            {"id": "z-ai/glm-4.7-flash", "label": "GLM 4.7 Flash"},
            {"id": "thinkingmachines/inkling-small", "label": "Inkling Small"},
        ]
        self._chat_open = False
        self._ops_open = False
        self._live_user = ""
        self._live_asst = ""
        self._live_visible = False
        self._tool_capsule = ""
        self._tool_capsule_status = ""
        self._stt_ms = 0.0
        self._llm_ms = 0.0
        self._llm_ttft_ms = 0.0
        self._llm_decode_ms = 0.0
        self._tts_ms = 0.0
        self._ttfa_ms = 0.0
        self._e2e_ms = 0.0
        self._stt_path = ""
        self._ns_strength = 0.5
        self._gate_rms = 0.016
        self._ns_on = True
        self._ns_timer = QTimer(self)
        self._ns_timer.setSingleShot(True)
        self._ns_timer.setInterval(350)
        self._ns_timer.timeout.connect(self._flush_ns_strength)
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
            if hasattr(bridge, "settingsChanged"):
                bridge.settingsChanged.connect(self._on_settings)
            if hasattr(bridge, "llmStatus"):
                bridge.llmStatus.connect(self._on_llm_status)
            if hasattr(bridge, "turnMetrics"):
                bridge.turnMetrics.connect(self._on_metrics)

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

    @Property(str, notify=llmModeChanged)
    def llmMode(self) -> str:
        return self._llm_mode

    @Property(str, notify=localHefChanged)
    def localHef(self) -> str:
        return self._local_hef

    @Property(str, notify=orModelChanged)
    def orModel(self) -> str:
        return self._or_model

    @Property(str, notify=llmStatusChanged)
    def llmStatus(self) -> str:
        return self._llm_status

    @Property(bool, notify=hasOrKeyChanged)
    def hasOrKey(self) -> bool:
        return self._has_or_key

    @Property(bool, notify=settingsOpenChanged)
    def settingsOpen(self) -> bool:
        return self._settings_open

    @Property("QVariantList", notify=localModelsChanged)
    def localModels(self) -> list:
        return self._local_models

    @Property("QVariantList", notify=orModelsChanged)
    def orModels(self) -> list:
        return self._or_models

    @Slot()
    def toggleSettings(self) -> None:
        self._settings_open = not self._settings_open
        if self._settings_open:
            self._chat_open = False
            self._ops_open = False
            self.chatOpenChanged.emit(False)
            self.opsOpenChanged.emit(False)
        self.settingsOpenChanged.emit(self._settings_open)

    @Slot()
    def toggleChat(self) -> None:
        self._chat_open = not self._chat_open
        if self._chat_open:
            self._settings_open = False
            self._ops_open = False
            self.settingsOpenChanged.emit(False)
            self.opsOpenChanged.emit(False)
        self.chatOpenChanged.emit(self._chat_open)

    @Slot()
    def toggleOps(self) -> None:
        self._ops_open = not self._ops_open
        if self._ops_open:
            self._settings_open = False
            self._chat_open = False
            self.settingsOpenChanged.emit(False)
            self.chatOpenChanged.emit(False)
        self.opsOpenChanged.emit(self._ops_open)

    @Property(bool, notify=chatOpenChanged)
    def chatOpen(self) -> bool:
        return self._chat_open

    @Property(bool, notify=opsOpenChanged)
    def opsOpen(self) -> bool:
        return self._ops_open

    @Property(str, notify=liveUserChanged)
    def liveUser(self) -> str:
        return self._live_user

    @Property(str, notify=liveAssistantChanged)
    def liveAssistant(self) -> str:
        return self._live_asst

    @Property(bool, notify=liveVisibleChanged)
    def liveVisible(self) -> bool:
        return self._live_visible

    @Property(str, notify=toolCapsuleChanged)
    def toolCapsule(self) -> str:
        return self._tool_capsule

    @Property(str, notify=toolCapsuleStatusChanged)
    def toolCapsuleStatus(self) -> str:
        return self._tool_capsule_status

    @Property(float, notify=sttMsChanged)
    def sttMs(self) -> float:
        return self._stt_ms

    @Property(float, notify=llmMsChanged)
    def llmMs(self) -> float:
        return self._llm_ms

    @Property(float, notify=llmTtftMsChanged)
    def llmTtftMs(self) -> float:
        return self._llm_ttft_ms

    @Property(float, notify=llmDecodeMsChanged)
    def llmDecodeMs(self) -> float:
        return self._llm_decode_ms

    @Property(float, notify=ttsMsChanged)
    def ttsMs(self) -> float:
        return self._tts_ms

    @Property(float, notify=ttfaMsChanged)
    def ttfaMs(self) -> float:
        return self._ttfa_ms

    @Property(float, notify=e2eMsChanged)
    def e2eMs(self) -> float:
        return self._e2e_ms

    @Property(str, notify=sttPathChanged)
    def sttPath(self) -> str:
        return self._stt_path

    @Property(float, notify=nsStrengthChanged)
    def nsStrength(self) -> float:
        return self._ns_strength

    @Property(float, notify=gateRmsChanged)
    def gateRms(self) -> float:
        return self._gate_rms

    @Property(bool, notify=nsOnChanged)
    def nsOn(self) -> bool:
        return self._ns_on

    @Slot()
    def closeDrawers(self) -> None:
        if self._settings_open:
            self._settings_open = False
            self.settingsOpenChanged.emit(False)
        if self._chat_open:
            self._chat_open = False
            self.chatOpenChanged.emit(False)
        if self._ops_open:
            self._ops_open = False
            self.opsOpenChanged.emit(False)

    @Slot(float)
    def setGateRms(self, value: float) -> None:
        self._gate_rms = float(value)
        self.gateRmsChanged.emit(self._gate_rms)
        if self._bridge is not None:
            self._bridge.set_voice_settings(gate_min_rms=self._gate_rms)
            thr = min(0.95, max(0.05, self._gate_rms / 0.08))
            self._bridge.set_vad_threshold(thr)

    @Slot(float)
    def setNsStrength(self, value: float) -> None:
        self._ns_strength = max(0.0, min(1.0, float(value)))
        self.nsStrengthChanged.emit(self._ns_strength)
        self._ns_timer.start()

    def _flush_ns_strength(self) -> None:
        if self._bridge is not None:
            self._bridge.set_voice_settings(ns_strength=self._ns_strength)

    @Slot(bool)
    def setNsOn(self, on: bool) -> None:
        self._ns_on = bool(on)
        self.nsOnChanged.emit(self._ns_on)
        if self._bridge is not None:
            self._bridge.set_voice_settings(ns="dtln" if on else "off")

    @Slot(str, str, str)
    def applyLlmSettings(self, mode: str, local_hef: str, or_model: str) -> None:
        if self._bridge is None:
            return
        self._bridge.set_settings(mode, local_hef, or_model)

    def _on_settings(self, payload: dict) -> None:
        mode = str(payload.get("mode") or "local")
        if mode != self._llm_mode:
            self._llm_mode = mode
            self.llmModeChanged.emit(mode)
        hef = str(payload.get("local_hef") or self._local_hef)
        if hef != self._local_hef:
            self._local_hef = hef
            self.localHefChanged.emit(hef)
        orm = str(payload.get("or_model") or self._or_model)
        if orm != self._or_model:
            self._or_model = orm
            self.orModelChanged.emit(orm)
        key = bool(payload.get("has_or_key"))
        if key != self._has_or_key:
            self._has_or_key = key
            self.hasOrKeyChanged.emit(key)
        if payload.get("ns_strength") is not None:
            try:
                self._ns_strength = float(payload["ns_strength"])
                self.nsStrengthChanged.emit(self._ns_strength)
            except (TypeError, ValueError):
                pass
        if payload.get("gate_min_rms") is not None:
            try:
                self._gate_rms = float(payload["gate_min_rms"])
                self.gateRmsChanged.emit(self._gate_rms)
            except (TypeError, ValueError):
                pass
        if payload.get("ns") is not None:
            self._ns_on = str(payload.get("ns")).lower() not in {"off", "none", "0"}
            self.nsOnChanged.emit(self._ns_on)
        loc = payload.get("local_models")
        if isinstance(loc, list) and loc:
            self._local_models = loc
            self.localModelsChanged.emit()
        orm_list = payload.get("or_models")
        if isinstance(orm_list, list) and orm_list:
            self._or_models = orm_list
            self.orModelsChanged.emit()
        tag = "Cloud" if mode == "openrouter" else "Local"
        model = orm if mode == "openrouter" else hef
        self._set_label(f"{tag} · {model}")

    def _on_llm_status(self, status: str) -> None:
        self._llm_status = status or ""
        self.llmStatusChanged.emit(self._llm_status)
        if status and status not in {"ready"}:
            self._set_label(status)

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
        st = (text or "").rsplit(" · ", 1)[-1].lower() if " · " in (text or "") else ""
        if st in {"running", "searching", "generating", "done", "failed", "complete", "completed"}:
            self._tool_capsule = text
            self._tool_capsule_status = st
            self.toolCapsuleChanged.emit(self._tool_capsule)
            self.toolCapsuleStatusChanged.emit(st)
            if st in {"running", "searching", "generating"}:
                self.transcriptAdded.emit("tool", text)

    def _on_user(self, text: str) -> None:
        t = (text or "").strip()
        if not t:
            return
        if self._is_echo(t):
            print(f"[hmi] drop echo transcript: {t!r}", flush=True)
            return
        self._live_user = t
        self._live_asst = ""
        self._live_visible = True
        self.liveUserChanged.emit(t)
        self.liveAssistantChanged.emit("")
        self.liveVisibleChanged.emit(True)
        self._tool_capsule = ""
        self._tool_capsule_status = ""
        self.toolCapsuleChanged.emit("")
        self.toolCapsuleStatusChanged.emit("")
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
        self._live_asst = self._asst
        self._live_visible = True
        self.liveAssistantChanged.emit(self._asst)
        self.liveVisibleChanged.emit(True)

    def _on_asst_done(self, text: str) -> None:
        final = (text or self._asst).strip()
        self._asst = ""
        if final:
            self._last_nova = final
            self._live_asst = final
            self._live_visible = True
            self.liveAssistantChanged.emit(final)
            self.liveVisibleChanged.emit(True)
            self.transcriptAdded.emit("nova", final)

    def _on_metrics(self, payload: dict) -> None:
        def _f(key, dest_attr, sig):
            v = payload.get(key)
            if v is None:
                return
            try:
                val = float(v)
            except (TypeError, ValueError):
                return
            setattr(self, dest_attr, val)
            sig.emit(val)

        _f("stt_ms", "_stt_ms", self.sttMsChanged)
        _f("llm_ms", "_llm_ms", self.llmMsChanged)
        if self._llm_ms <= 0:
            _f("llm_total_ms", "_llm_ms", self.llmMsChanged)
        if self._llm_ms <= 0:
            # OR logs had ttft+decode but never llm_ms
            ttft = payload.get("llm_ttft_ms") or payload.get("ttft_ms")
            dec = payload.get("llm_decode_ms") or payload.get("decode_ms")
            try:
                parts = [float(x) for x in (ttft, dec) if x is not None]
                if parts:
                    self._llm_ms = sum(parts)
                    self.llmMsChanged.emit(self._llm_ms)
            except (TypeError, ValueError):
                pass
        _f("llm_ttft_ms", "_llm_ttft_ms", self.llmTtftMsChanged)
        if self._llm_ttft_ms <= 0:
            _f("ttft_ms", "_llm_ttft_ms", self.llmTtftMsChanged)
        _f("llm_decode_ms", "_llm_decode_ms", self.llmDecodeMsChanged)
        if self._llm_decode_ms <= 0:
            _f("decode_ms", "_llm_decode_ms", self.llmDecodeMsChanged)
        _f("tts_ms", "_tts_ms", self.ttsMsChanged)
        _f("ttfa_ms", "_ttfa_ms", self.ttfaMsChanged)
        e2e = payload.get("speech_end_to_audible_ms") or payload.get("total_latency_ms") or payload.get("ttfa_ms")
        if e2e is not None:
            try:
                self._e2e_ms = float(e2e)
                self.e2eMsChanged.emit(self._e2e_ms)
                self._set_latency(int(self._e2e_ms))
            except (TypeError, ValueError):
                pass
        path = payload.get("stt_path") or payload.get("stt_engine")
        if path:
            self._stt_path = str(path)
            self.sttPathChanged.emit(self._stt_path)

    @Slot()
    def clearToolCapsule(self) -> None:
        if self._tool_capsule:
            self._tool_capsule = ""
            self._tool_capsule_status = ""
            self.toolCapsuleChanged.emit("")
            self.toolCapsuleStatusChanged.emit("")

    @Slot()
    def hideLive(self) -> None:
        if self._live_visible:
            self._live_visible = False
            self.liveVisibleChanged.emit(False)

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
