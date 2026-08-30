"""QWebSocket client for nova-hailo /v1/realtime (same protocol as the web client)."""
from __future__ import annotations

import base64
import json

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWebSockets import QWebSocket

_Unconnected = QAbstractSocket.SocketState.UnconnectedState
_Connecting = QAbstractSocket.SocketState.ConnectingState
_Closing = QAbstractSocket.SocketState.ClosingState
_Connected = QAbstractSocket.SocketState.ConnectedState

from nova_hmi.protocol import (
    assistant_delta,
    assistant_done,
    is_user_transcript,
    llm_status_label,
    map_fsm_to_phase,
    settings_payload,
    tool_status_label,
    turn_metrics_payload,
)


class RealtimeBridge(QObject):
    connected = Signal()
    disconnected = Signal()
    phaseReceived = Signal(str)
    fsmReceived = Signal(str)
    userTranscript = Signal(str)
    assistantDelta = Signal(str)
    assistantDone = Signal(str)
    audioDelta = Signal(bytes, int)
    playbackCancel = Signal()
    toolStatus = Signal(str)
    greeting = Signal(str)
    latencyMs = Signal(int)
    turnDone = Signal()
    failClosed = Signal(str)
    settingsChanged = Signal(dict)
    llmStatus = Signal(str)
    turnMetrics = Signal(dict)
    googleAuthUrl = Signal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url = QUrl(url)
        self._sock = QWebSocket()
        self._sock.connected.connect(self._on_connected)
        self._sock.disconnected.connect(self._on_disconnected)
        self._sock.textMessageReceived.connect(self._on_message)
        err_sig = getattr(self._sock, "errorOccurred", None) or getattr(
            self._sock, "error", None
        )
        if err_sig is not None:
            err_sig.connect(self._on_error)
        self._retry = QTimer(self)
        self._retry.setInterval(3000)
        self._retry.timeout.connect(self.open)
        self._want_open = False
        self.last_error = ""

    def open(self) -> None:
        self._want_open = True
        st = self._sock.state()
        if st in {_Connecting, _Connected, _Closing}:
            return
        print(f"[hmi] ws open {self._url.toString()}", flush=True)
        self._sock.open(self._url)

    def close(self) -> None:
        self._want_open = False
        self._retry.stop()
        self._sock.close()

    def send(self, payload: dict) -> None:
        if self._sock.state() == _Connected:
            self._sock.sendTextMessage(json.dumps(payload))

    def send_pcm16(self, pcm: bytes) -> None:
        if not pcm:
            return
        self.send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
        )

    def arm(self) -> None:
        self.send(
            {
                "type": "session.update",
                "session": {"arm": True, "ptt": True},
            }
        )

    def cancel(self) -> None:
        self.send({"type": "response.cancel"})

    def request_settings(self) -> None:
        self.send({"type": "nova.settings.get"})

    def set_settings(self, mode: str, local_hef: str, or_model: str) -> None:
        self.send(
            {
                "type": "nova.settings.set",
                "mode": mode,
                "local_hef": local_hef,
                "or_model": or_model,
            }
        )

    def connect_google(self) -> None:
        self.send({"type": "nova.google.connect"})

    def disconnect_google(self) -> None:
        self.send({"type": "nova.google.disconnect"})

    def set_voice_settings(self, *, gate_min_rms=None, ns=None, ns_strength=None) -> None:
        payload: dict = {"type": "nova.settings.set"}
        if gate_min_rms is not None:
            payload["gate_min_rms"] = float(gate_min_rms)
        if ns is not None:
            payload["ns"] = ns
        if ns_strength is not None:
            payload["ns_strength"] = float(ns_strength)
        self.send(payload)

    def set_vad_threshold(self, threshold: float) -> None:
        self.send(
            {
                "type": "session.update",
                "session": {
                    "audio": {
                        "input": {
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": float(threshold),
                            }
                        }
                    }
                },
            }
        )

    def playback_started(self, generation_id=None) -> None:
        self.send(
            {
                "type": "playback.started",
                "generation_id": generation_id,
                "t_ms": 0,
            }
        )

    def playback_interrupted(self) -> None:
        self.send({"type": "playback.interrupted", "reason": "client", "t_ms": 0})

    def _on_connected(self) -> None:
        self._retry.stop()
        print("[hmi] ws connected — arming session", flush=True)
        self.arm()
        self.request_settings()
        self.connected.emit()

    def _on_disconnected(self) -> None:
        print("[hmi] ws disconnected", flush=True)
        self.disconnected.emit()
        if self._want_open:
            self._retry.start()

    def _on_error(self, *_args) -> None:
        self.last_error = self._sock.errorString()
        print(f"[hmi] ws error: {self.last_error}", flush=True)

    def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        kind = msg.get("type")
        if kind == "session.created":
            sess = msg.get("session") or {}
            fsm = sess.get("fsm") or {}
            if isinstance(fsm, dict) and fsm.get("state"):
                self.fsmReceived.emit(str(fsm["state"]))
                self.phaseReceived.emit(map_fsm_to_phase(str(fsm["state"])))
            greet = str(sess.get("greeting") or "").strip()
            if greet:
                self.greeting.emit(greet)
            return
        if kind == "nova.fsm":
            fsm = msg.get("fsm") or {}
            state = str(fsm.get("state") or "")
            if state:
                self.fsmReceived.emit(state)
                self.phaseReceived.emit(map_fsm_to_phase(state))
            return
        if kind == "nova.google.auth_url":
            url = str(msg.get("auth_url") or "").strip()
            if url:
                self.googleAuthUrl.emit(url)
            else:
                err = msg.get("error") or "no auth_url returned"
                print(f"[hmi] google oauth start failed: {err}", flush=True)
            return
        user = is_user_transcript(msg)
        if user:
            self.userTranscript.emit(user)
            self.phaseReceived.emit("thinking")
            return
        delta = assistant_delta(msg)
        if delta is not None:
            self.assistantDelta.emit(delta)
            return
        done = assistant_done(msg)
        if done is not None:
            self.assistantDone.emit(done)
            return
        if kind in {"response.audio.delta", "response.output_audio.delta"}:
            b64 = msg.get("delta") or ""
            try:
                pcm = base64.b64decode(b64)
            except Exception:
                return
            rate = int(msg.get("sample_rate") or msg.get("sampleRate") or 24000)
            self.audioDelta.emit(pcm, rate)
            self.phaseReceived.emit("speaking")
            return
        if kind == "playback.cancel":
            self.playbackCancel.emit()
            self.phaseReceived.emit("listening")
            return
        if kind == "input_audio_buffer.speech_started":
            self.phaseReceived.emit("listening")
            return
        if kind == "input_audio_buffer.speech_stopped":
            self.phaseReceived.emit("thinking")
            return
        if kind == "response.done":
            meta = ((msg.get("response") or {}).get("metadata") or {})
            ttfa = meta.get("ttfa_ms")
            if ttfa is not None:
                try:
                    self.latencyMs.emit(int(float(ttfa)))
                except (TypeError, ValueError):
                    pass
            if meta.get("fail_closed") or meta.get("skipped"):
                reason = str(meta.get("reason") or "fail_closed")
                print(f"[hmi] fail_closed reason={reason}", flush=True)
                self.failClosed.emit(reason)
            self.turnDone.emit()
            return
        label = tool_status_label(msg)
        if label:
            self.toolStatus.emit(label)
            self.phaseReceived.emit("thinking")
            return
        if kind == "nova.research_status":
            self.toolStatus.emit(f"research · {msg.get('status') or 'running'}")
            self.phaseReceived.emit("thinking")
            return
        metrics = turn_metrics_payload(msg)
        if metrics is not None:
            self.turnMetrics.emit(metrics)
            return
        settings = settings_payload(msg)
        if settings is not None:
            self.settingsChanged.emit(settings)
            return
        st = llm_status_label(msg)
        if st is not None:
            self.llmStatus.emit(st)
            if st not in {"ready", "loading", "queued"}:
                self.toolStatus.emit(st)
            return
        if kind == "error":
            err = msg.get("error") or {}
            detail = err.get("message") or err.get("type") or "error"
            print(f"[hmi] server error: {detail}", flush=True)
            self.last_error = str(detail)
            self.toolStatus.emit(str(detail))
            self.phaseReceived.emit("error")
