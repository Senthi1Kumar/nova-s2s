"""Nova HMI protocol mapper — no Qt, no Hailo."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hmi_qt"))

from nova_hmi.protocol import (
    assistant_delta,
    assistant_done,
    integrations_payload,
    is_user_transcript,
    llm_status_label,
    map_fsm_to_phase,
    settings_payload,
    tool_status_label,
    turn_metrics_payload,
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


def test_resample_16k_to_48k_is_exact_triple():
    from nova_hmi.audio_gain import resample_int16

    pcm = (b"\x00\x10" * 100)
    out = resample_int16(pcm, 16000, 48000)
    assert len(out) == 100 * 3 * 2


def test_manual_gain_locked_agc_off():
    from nova_hmi.audio_gain import AGC_ENABLED, MANUAL_GAIN, uplink_multiplier

    assert AGC_ENABLED is False
    assert 3.0 <= MANUAL_GAIN <= 4.0
    assert uplink_multiplier(rms=0.05, agc=8.0) == MANUAL_GAIN
    assert uplink_multiplier(rms=0.2, agc=1.0) == MANUAL_GAIN


def test_pcm16_rms_and_dead_capture():
    from nova_hmi.audio_gain import (
        capture_looks_dead,
        pcm16_rms,
        playback_done_debounce_s,
    )

    silent = b"\x00\x00" * 160
    speech = b"\x00\x40" * 160
    assert pcm16_rms(silent) < 1e-4
    assert pcm16_rms(speech) > 0.001
    assert capture_looks_dead(rms=0.0, blocked=False) is True
    assert capture_looks_dead(rms=0.0, blocked=True) is False
    assert capture_looks_dead(rms=0.02, blocked=False) is False
    assert (
        playback_done_debounce_s(
            queue_empty=True, started_play=True, idle_s=0.05, hold_s=0.35
        )
        is False
    )
    assert (
        playback_done_debounce_s(
            queue_empty=True, started_play=True, idle_s=0.4, hold_s=0.35
        )
        is True
    )
    assert (
        playback_done_debounce_s(
            queue_empty=False, started_play=True, idle_s=1.0, hold_s=0.35
        )
        is False
    )


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
    assert settings_payload({"type": "nova.settings", "mode": "local"})["mode"] == "local"
    assert llm_status_label({"type": "nova.llm_status", "status": "ready"}) == "ready"
    assert turn_metrics_payload({"type": "nova.turn_metrics", "stt_ms": 12})["stt_ms"] == 12


def test_integrations_payload_reads_connected_and_healthy():
    out = integrations_payload(
        {
            "type": "nova.settings",
            "google": {"connected": True, "needs_reauth": False},
            "connectors": {"enabled": 3, "tools": 11},
        }
    )
    assert out == {
        "google_connected": True,
        "google_needs_reauth": False,
        "connectors_enabled": 3,
        "connectors_tools": 11,
    }


def test_integrations_payload_needs_reauth_does_not_read_as_connected():
    # A revoked token still leaves google.connected true on the wire, so the
    # HMI must key off needs_reauth, not just connected, before showing
    # "Connected". This asserts the raw parse only -- controller.py is where
    # the two are combined into "Connected"/"Reconnect needed"/"Disconnected".
    out = integrations_payload(
        {
            "type": "nova.settings",
            "google": {"connected": True, "needs_reauth": True},
            "connectors": {"enabled": 0, "tools": 0},
        }
    )
    assert out["google_connected"] is True
    assert out["google_needs_reauth"] is True


def test_integrations_payload_ignores_non_settings_messages():
    assert integrations_payload({"type": "nova.fsm"}) is None


def test_integrations_payload_tolerates_an_older_backend_missing_the_keys():
    # An older backend that has not been upgraded to send "google"/
    # "connectors" yet must not raise -- it degrades to a disconnected/
    # empty snapshot instead.
    out = integrations_payload({"type": "nova.settings", "mode": "local"})
    assert out == {
        "google_connected": False,
        "google_needs_reauth": False,
        "connectors_enabled": 0,
        "connectors_tools": 0,
    }


def test_integrations_payload_tolerates_malformed_values():
    out = integrations_payload(
        {
            "type": "nova.settings",
            "google": "not-a-dict",
            "connectors": {"enabled": "oops", "tools": None},
        }
    )
    assert out == {
        "google_connected": False,
        "google_needs_reauth": False,
        "connectors_enabled": 0,
        "connectors_tools": 0,
    }
