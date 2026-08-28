"""Map nova-hailo /v1/realtime FSM + events onto the QML phase vocabulary.

QML phases: idle | listening | thinking | speaking | error.
"""
from __future__ import annotations

PHASES = ("idle", "listening", "thinking", "speaking", "error")

_FSM_TO_PHASE = {
    "IDLE": "idle",
    "ARMED": "idle",
    "CONNECTING": "idle",
    "LISTENING": "listening",
    "TRANSCRIBING": "thinking",
    "THINKING": "thinking",
    "SPEAKING": "speaking",
    "INTERRUPTING": "listening",
}


def map_fsm_to_phase(state: str) -> str:
    return _FSM_TO_PHASE.get((state or "").strip().upper(), "idle")


def is_user_transcript(msg: dict) -> str | None:
    if msg.get("type") != "conversation.item.input_audio_transcription.completed":
        return None
    t = str(msg.get("transcript") or "").strip()
    return t or None


def assistant_delta(msg: dict) -> str | None:
    if msg.get("type") not in {
        "response.audio_transcript.delta",
        "response.output_audio_transcript.delta",
    }:
        return None
    d = msg.get("delta")
    return None if d is None else str(d)


def assistant_done(msg: dict) -> str | None:
    if msg.get("type") not in {
        "response.audio_transcript.done",
        "response.output_audio_transcript.done",
    }:
        return None
    t = str(msg.get("transcript") or "").strip()
    return t or None


def llm_status_label(msg: dict) -> str | None:
    if msg.get("type") != "nova.llm_status":
        return None
    status = str(msg.get("status") or "").strip()
    err = str(msg.get("error") or "").strip()
    if status == "error" and err:
        return err
    return status or None


def turn_metrics_payload(msg: dict) -> dict | None:
    if msg.get("type") not in {"nova.turn_metrics", "nova.metrics"}:
        return None
    if msg.get("type") == "nova.metrics":
        return msg.get("turn") if isinstance(msg.get("turn"), dict) else msg
    return msg


def settings_payload(msg: dict) -> dict | None:
    if msg.get("type") != "nova.settings":
        return None
    return msg


def tool_status_label(msg: dict) -> str | None:
    if msg.get("type") != "nova.tool_status":
        return None
    name = str(msg.get("name") or "tool")
    status = str(msg.get("status") or "")
    return f"{name} · {status}".strip(" ·")
