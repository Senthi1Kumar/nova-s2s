"""Microphone-specific capture routes shared by setup, telemetry, and clients."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_CARD_LINE = re.compile(r"^\s*\d+\s+\[[^\]]*\]\s*:\s*(?P<driver>.*?)(?:\s+-\s+(?P<name>.*))?$")


@dataclass(frozen=True)
class AudioProfile:
    kind: str
    capture_device: str
    playback_device: str
    capture_node: str
    playback_node: str
    input_channels: int
    input_index: int
    gain: float
    processing: str


def _find(devices: list[dict[str, Any]], *needles: str) -> dict[str, Any] | None:
    for device in devices:
        name = str(device.get("name", ""))
        if any(needle.lower() in name.lower() for needle in needles):
            return device
    return None


def parse_asound_cards(text: str) -> list[dict[str, Any]]:
    """Parse /proc/asound/cards into the device dicts detect_audio_profile expects."""
    devices: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _CARD_LINE.match(line)
        if match is None:
            continue
        name = (match.group("name") or match.group("driver") or "").strip()
        if name:
            devices.append(
                {"name": name, "max_input_channels": 2, "max_output_channels": 2}
            )
    return devices


def _env_float(env: Mapping[str, str], key: str) -> float | None:
    try:
        value = float(env.get(key, ""))
    except ValueError:
        return None
    return value if value > 0 else None


def detect_audio_profile(
    env: Mapping[str, str], devices: Iterable[dict[str, Any]]
) -> AudioProfile:
    available = list(devices)
    xvf = _find(available, "xvf3800", "reSpeaker")
    if xvf is not None:
        name = str(xvf["name"])
        channels = 2 if int(xvf.get("max_input_channels", 1)) >= 2 else 1
        index = 0 if channels < 2 or env.get("AIBOX_XVF_CHANNEL", "").strip() == "0" else 1
        gain = _env_float(env, "NOVA_MIC_GAIN") or (3.0 if index == 1 else 1.0)
        return AudioProfile(
            kind="xvf3800",
            capture_device=env.get("NOVA_CAPTURE_DEVICE", name),
            playback_device=env.get("NOVA_PLAYBACK_DEVICE", name),
            capture_node=name,
            playback_node=name,
            input_channels=channels,
            input_index=index,
            gain=gain,
            processing=(
                "XVF3800 onboard AEC; ch1 is the echo-cancelled single-mic residual (no beamforming, noise suppression, or AGC)"
                if index == 1
                else "XVF3800 onboard AEC; ch0 is the processed (AGC) channel"
            ),
        )
    wm = _find(available, "wm8960")
    if wm is not None:
        return AudioProfile(
            kind="wm8960",
            capture_device=env.get("NOVA_CAPTURE_DEVICE", "pulse"),
            playback_device=env.get("NOVA_PLAYBACK_DEVICE", "pulse"),
            capture_node="echo-cancel-source",
            playback_node="echo-cancel-sink",
            input_channels=1,
            input_index=0,
            gain=1.0,
            processing="PipeWire WebRTC AEC with the AI Box playback reference",
        )
    first = available[0] if available else {"name": "default"}
    name = str(first.get("name", "default"))
    channels = max(1, int(first.get("max_input_channels", 1)))
    return AudioProfile(
        kind="generic",
        capture_device=env.get("NOVA_CAPTURE_DEVICE", name),
        playback_device=env.get("NOVA_PLAYBACK_DEVICE", name),
        capture_node=name,
        playback_node=name,
        input_channels=channels,
        input_index=0,
        gain=1.0,
        processing="platform/default audio processing",
    )


def shell_exports(
    env: Mapping[str, str], devices: Iterable[dict[str, Any]]
) -> list[str]:
    """Shell lines exporting the profile's audio env; variables already set in env are kept."""
    profile = detect_audio_profile(env, devices)
    values: dict[str, str] = {"AIBOX_AUDIO_PROFILE": profile.kind}
    if profile.kind != "generic":
        values["NOVA_CAPTURE_DEVICE"] = profile.capture_device
        values["NOVA_PLAYBACK_DEVICE"] = profile.playback_device
        values["NOVA_CAPTURE_CHANNEL"] = str(profile.input_index)
        values["NOVA_MIC_GAIN"] = f"{profile.gain:g}"
    if profile.kind == "wm8960":
        values["PULSE_SOURCE"] = profile.capture_node
        values["PULSE_SINK"] = profile.playback_node
    lines = [
        f"export {key}={shlex.quote(value)}"
        for key, value in values.items()
        if not env.get(key)
    ]
    summary = (
        f"audio: {profile.kind} capture={profile.capture_device} "
        f"ch={profile.input_index} gain={profile.gain:g} playback={profile.playback_device}"
    )
    lines.append(f"echo {shlex.quote(summary)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect the audio profile.")
    parser.add_argument("--shell", action="store_true", help="print export lines for eval")
    parser.add_argument("--cards", default="/proc/asound/cards", help="ALSA cards file")
    args = parser.parse_args(argv)
    try:
        with open(args.cards, encoding="utf-8") as fh:
            devices = parse_asound_cards(fh.read())
    except OSError:
        devices = []
    if args.shell:
        print("\n".join(shell_exports(os.environ, devices)))
    else:
        print(detect_audio_profile(os.environ, devices))
    return 0


if __name__ == "__main__":
    sys.exit(main())
