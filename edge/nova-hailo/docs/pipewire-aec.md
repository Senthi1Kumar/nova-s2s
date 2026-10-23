# PipeWire echo-cancel (WM8960 / Pi HMI)

This is the WM8960 path. A reSpeaker XVF3800 cancels echo on the board; `scripts/setup_pipewire_aec.sh` removes this module when that card is present, so the same echo is not processed twice.

Zero-Python AEC for the Qt HMI on a shared-codec WM8960 (same clock for speaker
and mic). Playback goes to the echo-cancel **sink**; capture comes from the
echo-cancel **source**. In-process AEC (TTS PCM as far-end) is a fallback only
if this ERLE is not enough.

## Setup

`./scripts/run_demo_oem.sh` writes the PipeWire conf (restart only if it
changed) and sources `scripts/hmi_aec_env.sh`. `./hmi_qt/run.sh` sources the
same env in the HMI process (it does not inherit the demo shell). Skip with
`NOVA_HAILO_AEC=0`.

Soak defaults from `hmi_aec_env.sh` (override in the shell before launching):

```bash
PULSE_SOURCE=echo-cancel-source
PULSE_SINK=echo-cancel-sink
NOVA_HAILO_CAPTURE_DEVICE=pulse
NOVA_HAILO_PLAYBACK_DEVICE=pulse
NOVA_HAILO_ECHO_TAIL_S=0.15
NOVA_HAILO_BARGE_IN_WHILE_SPEAKING=true
```

PortAudio cannot open PipeWire node names. The HMI opens the Pulse plugin
device (`pulse`) and Pulse routes to the AEC nodes via `PULSE_SOURCE` /
`PULSE_SINK`.

YAML `voice.barge_in_while_speaking` stays **false**; the launcher env is the
soak switch. After soak, you can bake `true` into `config.oem_v1.1.yaml`.

One-off / debug (not required for a normal demo):

```bash
./scripts/setup_pipewire_aec.sh
```

## What the module does

`libpipewire-module-echo-cancel` (default library `aec/libspa-aec-webrtc`,
AEC3) creates:

| Node | Role |
| --- | --- |
| `echo-cancel-sink` | Apps play here (HMI TTS) |
| `echo-cancel-source` | Apps capture here (HMI mic uplink) |
| capture / playback streams | Tied to the WM8960 hardware ports |

Apps must **not** open the raw WM8960 while AEC is in use, or the far-end
reference never matches what the speaker plays.
