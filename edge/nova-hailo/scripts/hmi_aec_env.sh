# Shared HMI/backend AEC env. Sourced by run_demo_oem.sh and hmi_qt/run.sh.
# Override any value in the shell before launching.
#
# PortAudio does not see PipeWire node names (echo-cancel-source). The Pulse
# plugin device is "pulse"; PULSE_SOURCE / PULSE_SINK select the AEC nodes.
export PULSE_SOURCE="${PULSE_SOURCE:-echo-cancel-source}"
export PULSE_SINK="${PULSE_SINK:-echo-cancel-sink}"
_cap="${NOVA_HAILO_CAPTURE_DEVICE:-pulse}"
_play="${NOVA_HAILO_PLAYBACK_DEVICE:-pulse}"
case "$_cap" in
  echo-cancel-source|echo-cancel-*) _cap=pulse ;;
esac
case "$_play" in
  echo-cancel-sink|echo-cancel-*) _play=pulse ;;
esac
export NOVA_HAILO_CAPTURE_DEVICE="$_cap"
export NOVA_HAILO_PLAYBACK_DEVICE="$_play"
unset _cap _play
# Barge-in and the echo tail move together, and neither is defaulted here any
# more. The env var overrides the profile, so defaulting it to true turned VAD
# barge-in on for *every* launch -- not just the soak launcher this comment used
# to describe. On the WM8960 the AEC does not reach enough echo-return-loss for
# that: Nova heard its own first syllables, speech_started fired with
# fsm=SPEAKING, and the turn aborted after one or two words.
#
# Unset, the profile decides (voice.barge_in_while_speaking, voice.echo_tail_ms).
# For a self-barge soak, opt in explicitly on the command line:
#   NOVA_HAILO_BARGE_IN_WHILE_SPEAKING=1 NOVA_HAILO_ECHO_TAIL_S=0.15 ./run.sh
# The short tail belongs with barge-in: half-duplex needs the longer profile
# value, or the mic reopens while the speaker is still ringing.
