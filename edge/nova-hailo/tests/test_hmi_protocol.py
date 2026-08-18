"""Nova HMI protocol mapper — no Qt, no Hailo."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hmi_qt"))

from nova_hmi.protocol import (
    assistant_delta,
    assistant_done,
    is_user_transcript,
    map_fsm_to_phase,
    tool_status_label,
)


def test_fsm_maps_to_qml_phases():
    assert map_fsm_to_phase("LISTENING") == "listening"
    assert map_fsm_to_phase("THINKING") == "thinking"
    assert map_fsm_to_phase("TRANSCRIBING") == "thinking"
    assert map_fsm_to_phase("SPEAKING") == "speaking"
    assert map_fsm_to_phase("IDLE") == "idle"
    assert map_fsm_to_phase("ARMED") == "idle"
    assert map_fsm_to_phase("INTERRUPTING") == "listening"


def test_transcript_and_tool_extractors():
    assert (
        is_user_transcript(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "hello there",
            }
        )
        == "hello there"
    )
    assert is_user_transcript({"type": "nova.fsm"}) is None
    assert assistant_delta({"type": "response.audio_transcript.delta", "delta": "Hi"}) == "Hi"
    assert assistant_done({"type": "response.audio_transcript.done", "transcript": "Hi."}) == "Hi."
    assert (
        tool_status_label({"type": "nova.tool_status", "name": "web_search", "status": "running"})
        == "web_search · running"
    )
    assert tool_status_label({"type": "nova.fsm"}) is None
