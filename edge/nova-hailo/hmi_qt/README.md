# Nova HMI (Qt 6 / QML)

Official driver UI. Same `/v1/realtime` backend as the web UI
(mic PCM in, TTS PCM out). **Python / PySide6** — do not CMake-build this
for the demo.

One voice session at a time: close the browser tab on `:8766` first.

## Car / this-Pi (default)

Backend and HMI both on the device that has the Hailo HAT. Audio and
display are **this** Pi (HDMI + WM8960 / USB). No tunnel.

```bash
# already: ./scripts/run_demo_oem.sh  (from nova-s2s/edge/nova-hailo)
cd hmi_qt
./run.sh
```

## Dev laptop only (optional)

```bash
ssh -L 8766:localhost:8766 <user>@<pi>
# other terminal, ON THE LAPTOP:
cd edge/nova-hailo/hmi_qt && ./run.sh
# or LAN: NOVA_HAILO_WS_URL=ws://<pi-ip>:8766/v1/realtime ./run.sh
```

Audio then follows the laptop. Running `./run.sh` *inside* SSH on the Pi
puts the UI on Pi HDMI and audio on the **Pi**, not the laptop.

`Esc` quits. `--kiosk` is fullscreen. `--demo` is a scripted orb, no backend.

Use the nova-hailo venv (`source ../scripts/setup_env.sh`). PySide6, numpy,
and sounddevice are in the parent `pyproject.toml` — one install for backend
and HMI. `hmi_qt/requirements.txt` is only a fallback.

## Layout

```
hmi_qt/
  run.sh              launcher (live WS + mic)
  main.py             Qt entry
  requirements.txt
  NovaUI/             QML (orb, transcript, theme)
  nova_hmi/           Python: audio, websocket, controller
  docs/PROTOCOL.md    /v1/realtime subset
```

## Controls

| Input | Action |
|---|---|
| Esc | quit |
| Speak into the mic | VAD on the backend Pi starts a turn |

Live Listen / Think / Speak is the orb + status strip (backend phase).
There are no on-screen phase buttons.
