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


def test_level_from_rms_silence_low_speech_high():
    from nova_hmi.audio_gain import level_from_rms

    assert level_from_rms(0.0005) < 0.15
    assert level_from_rms(0.05) > 0.4
    assert level_from_rms(0.05) < 0.9
    assert level_from_rms(0.4) > 0.85


def test_pick_input_only_when_default_is_wide():
    """128-ch Pulse default is the AGC bug; a normal 1–2ch default must stay
    so TTS/playback keep using Pulse resampling (16 kHz ALSA is silent)."""
    from nova_hmi.audio_gain import pick_input_index

    devices = [
        {"name": "pulse", "max_input_channels": 128, "max_output_channels": 128},
        {
            "name": "wm8960-soundcard: - (hw:2,0)",
            "max_input_channels": 2,
            "max_output_channels": 2,
        },
        {"name": "vc4-hdmi", "max_input_channels": 0, "max_output_channels": 8},
    ]
    assert pick_input_index(devices, default_channels=2) is None
    assert pick_input_index(devices, default_channels=128) == 1


def test_quiet_attack_is_still_sent():
    """WM8960 ALC starts quiet; dropping those frames chops the first words."""
    from nova_hmi.audio_gain import should_send_uplink

    assert should_send_uplink(rms=0.001, blocked=False, muted=False) is True
    assert should_send_uplink(rms=0.05, blocked=False, muted=False) is True
    assert should_send_uplink(rms=0.05, blocked=True, muted=False) is False
    assert should_send_uplink(rms=0.05, blocked=False, muted=True) is False


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
