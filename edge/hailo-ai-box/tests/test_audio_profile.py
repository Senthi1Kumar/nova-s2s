from aibox.audio.profile import detect_audio_profile


def _devices(*names):
    return [
        {"name": name, "max_input_channels": 2, "max_output_channels": 2}
        for name in names
    ]


def test_xvf3800_is_preferred_and_uses_processed_channel():
    profile = detect_audio_profile({}, _devices("default", "wm8960", "Seeed XVF3800"))
    assert profile.kind == "xvf3800"
    assert profile.capture_device == "Seeed XVF3800"
    assert profile.playback_device == "Seeed XVF3800"
    assert profile.input_index == 1
    assert profile.gain == 3.0
    assert "onboard AEC" in profile.processing
    assert "residual" in profile.processing
    assert "no beamforming" in profile.processing


def test_wm8960_uses_shared_pipewire_aec_route_without_xvf_gain():
    profile = detect_audio_profile({}, _devices("wm8960", "Seeed XVF3800"))
    assert profile.kind == "xvf3800"
    profile = detect_audio_profile({}, _devices("wm8960", "default"))
    assert profile.kind == "wm8960"
    assert profile.capture_device == "pulse"
    assert profile.playback_device == "pulse"
    assert profile.capture_node == "echo-cancel-source"
    assert profile.playback_node == "echo-cancel-sink"
    assert profile.input_index == 0
    assert profile.gain == 1.0


def test_generic_devices_and_explicit_overrides_remain_usable():
    profile = detect_audio_profile({}, _devices("USB Microphone"))
    assert profile.kind == "generic"
    assert profile.capture_device == "USB Microphone"
    assert profile.input_index == 0
    assert profile.gain == 1.0

    profile = detect_audio_profile(
        {"NOVA_CAPTURE_DEVICE": "Custom Mic", "NOVA_PLAYBACK_DEVICE": "Custom Speaker"},
        _devices("USB Microphone", "Custom Mic", "Custom Speaker"),
    )
    assert profile.capture_device == "Custom Mic"
    assert profile.playback_device == "Custom Speaker"


def test_audio_settings_env():
    from aibox.config import load_settings

    settings = load_settings({
        "AIBOX_AUDIO_PROFILE": "wm8960",
        "NOVA_CAPTURE_DEVICE": "pulse",
        "NOVA_PLAYBACK_DEVICE": "pulse",
    })
    assert settings.audio_profile == "wm8960"
    assert settings.audio_capture_device == "pulse"
    assert settings.audio_playback_device == "pulse"


XVF_CARDS = (
    " 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0\n"
    "                      vc4-hdmi-0\n"
    " 2 [Array          ]: USB-Audio - reSpeaker XVF3800 4-Mic Array\n"
    "                      Seeed reSpeaker XVF3800 4-Mic Array at usb-0000:01:00.0-1, high speed\n"
)
WM_CARDS = " 1 [wm8960soundcard]: wm8960-soundcard - wm8960-soundcard\n                      wm8960-soundcard\n"


def test_parse_asound_cards():
    from aibox.audio.profile import parse_asound_cards

    names = [d["name"] for d in parse_asound_cards(XVF_CARDS)]
    assert names == ["vc4-hdmi-0", "reSpeaker XVF3800 4-Mic Array"]
    assert [d["name"] for d in parse_asound_cards(WM_CARDS)] == ["wm8960-soundcard"]
    assert detect_audio_profile({}, parse_asound_cards(XVF_CARDS + WM_CARDS)).kind == "xvf3800"
    assert detect_audio_profile({}, parse_asound_cards(WM_CARDS)).kind == "wm8960"
    assert parse_asound_cards("--- no soundcards ---\n") == []


def _shell(tmp_path, monkeypatch, cards, **env):
    import io
    from contextlib import redirect_stdout

    from aibox.audio.profile import main

    for key in ("AIBOX_AUDIO_PROFILE", "NOVA_CAPTURE_DEVICE", "NOVA_PLAYBACK_DEVICE",
                "NOVA_CAPTURE_CHANNEL", "NOVA_MIC_GAIN", "AIBOX_XVF_CHANNEL",
                "PULSE_SOURCE", "PULSE_SINK"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "cards"
    path.write_text(cards)
    out = io.StringIO()
    with redirect_stdout(out):
        assert main(["--shell", "--cards", str(path)]) == 0
    return out.getvalue()


def test_shell_xvf_quotes_names_and_defaults_to_channel_1(tmp_path, monkeypatch):
    out = _shell(tmp_path, monkeypatch, XVF_CARDS)
    assert "export AIBOX_AUDIO_PROFILE=xvf3800" in out
    assert "export NOVA_CAPTURE_DEVICE='reSpeaker XVF3800 4-Mic Array'" in out
    assert "export NOVA_CAPTURE_CHANNEL=1" in out
    assert "export NOVA_MIC_GAIN=3" in out
    assert "PULSE_SOURCE" not in out
    assert "audio: xvf3800" in out


def test_shell_wm8960_exports_echo_cancel_route(tmp_path, monkeypatch):
    out = _shell(tmp_path, monkeypatch, WM_CARDS)
    assert "export AIBOX_AUDIO_PROFILE=wm8960" in out
    assert "export NOVA_CAPTURE_DEVICE=pulse" in out
    assert "export PULSE_SOURCE=echo-cancel-source" in out
    assert "export PULSE_SINK=echo-cancel-sink" in out


def test_shell_generic_and_no_cards_omit_device_vars(tmp_path, monkeypatch):
    for cards in ("", "--- no soundcards ---\n"):
        out = _shell(tmp_path, monkeypatch, cards)
        assert "export AIBOX_AUDIO_PROFILE=generic" in out
        assert "NOVA_CAPTURE_DEVICE" not in out


def test_xvf_channel_override_and_explicit_gain(tmp_path, monkeypatch):
    out = _shell(tmp_path, monkeypatch, XVF_CARDS, AIBOX_XVF_CHANNEL="0")
    assert "export NOVA_CAPTURE_CHANNEL=0" in out
    assert "export NOVA_MIC_GAIN=1" in out
    profile = detect_audio_profile({"AIBOX_XVF_CHANNEL": "0", "NOVA_MIC_GAIN": "2.5"},
                                   _devices("Seeed XVF3800"))
    assert (profile.input_index, profile.gain) == (0, 2.5)


def test_shell_keeps_existing_env(tmp_path, monkeypatch):
    out = _shell(tmp_path, monkeypatch, XVF_CARDS, NOVA_CAPTURE_DEVICE="Custom")
    assert "export NOVA_CAPTURE_DEVICE" not in out
    assert "export NOVA_PLAYBACK_DEVICE" in out
