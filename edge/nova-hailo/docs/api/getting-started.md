# Getting started: Nova on the Pi, a client on your device

You need a Raspberry Pi 5 with the AI HAT+ 2 running nova-hailo, and a device
with a microphone and a speaker on the same network. Three steps: start Nova
on the Pi, check it from a laptop, then set up your device.

## 1. Start Nova on the Pi

Once:

1. Get the code and its environment (the README covers the HailoRT base install):

   ```bash
   git clone -b share/nova-hailo-poc-v002 https://github.com/Senthi1Kumar/nova-s2s.git
   cd nova-s2s/edge/nova-hailo
   source scripts/setup_env.sh      # first run: venv + dependencies, slow
   ```

2. Put your LLM and search API keys in `.env` (see the README).
3. Give the Pi a fixed address (a DHCP reservation on your router). Devices
   trust a certificate that names this address.

Every time:

```bash
./scripts/run_demo_oem.sh
```

The first run makes a TLS certificate and an API token in
`~/.config/nova-hailo/` and loads them; later runs reuse them. The Pi is ready
when the log shows `Uvicorn running on https://0.0.0.0:8767`.

- If the Pi's address changes, the launcher makes a new certificate on its
  next start. Copy it to your devices again.
- One client at a time: close the Qt HMI or the web page before a device connects.
- `NOVA_HAILO_TLS=off ./scripts/run_demo_oem.sh` runs without certificate and
  token (plain `ws://`, lab use only).

Every device needs two things from the Pi: the certificate
`~/.config/nova-hailo/tls/cert.pem` and the token in
`~/.config/nova-hailo/api_token`. Never commit the token.

## 2. Check the Pi from a laptop (2 minutes)

The Python reference client is the tester: if it gets an answer, the Pi and
the network are fine, and any later problem is on the device side. It works
on any Linux or macOS laptop (or another Pi).

```bash
# <pi> is how you SSH to the Pi (e.g. cariad@192.168.1.36 or an ~/.ssh/config
# alias); <pi-ip> is its network address (e.g. 192.168.1.36).

# 1. Install the client's two libraries: WebSockets for the connection,
#    sounddevice for the mic and speaker (macOS: brew install portaudio first).
pip install "websockets>=13" sounddevice

# 2. Copy the Pi's certificate (public, not a secret). The client trusts exactly
#    this certificate, so it knows it is talking to your Pi before it sends the token.
scp <pi>:.config/nova-hailo/tls/cert.pem nova-cert.pem

# 3. Start the client, in one of two ways. Each reads the API token (secret)
#    from the Pi into this shell without printing it ("command cat" skips any
#    shell alias for cat) and starts the client in the same command:
#      --url    the Pi's secure realtime address (wss = WebSocket over TLS)
#      --token  proves this device may connect
#      --ca     trust only the certificate copied in step 2
#      --wake   who notices you want to talk (3a or 3b)

# 3a. Push to talk: press Enter, then speak. The mic is sent only after Enter,
#     like a device with a talk button.
export NOVA_HAILO_API_TOKEN=$(ssh <pi> command cat .config/nova-hailo/api_token) && \
python examples/realtime/python/nova_client.py --url wss://<pi-ip>:8767/v1/realtime \
  --token "$NOVA_HAILO_API_TOKEN" --ca nova-cert.pem --wake device

# 3b. Hands-free: say "Hey Nova, ..." at any time. The mic streams all the time
#     and the Pi listens for the wake word. After a reply, ask a follow-up
#     without it, until Nova goes idle again.
export NOVA_HAILO_API_TOKEN=$(ssh <pi> command cat .config/nova-hailo/api_token) && \
python examples/realtime/python/nova_client.py --url wss://<pi-ip>:8767/v1/realtime \
  --token "$NOVA_HAILO_API_TOKEN" --ca nova-cert.pem --wake server
```

Run each `export … && python …` block as one command. Push to talk suits
battery devices (nothing is sent while idle); hands-free suits mains-powered
ones.
To see Nova's screen as well, build `examples/realtime/lvgl` (its README) and
add `--ui examples/realtime/lvgl/build/nova_face`.

## 3. Set up your device

### Any device: let a coding agent build the client

Open a coding agent (for e.g., Claude Code/Codex) in this repo and say:

> Use `skills/nova-realtime-client` to set up a client for my <device>.

Add "and `skills/nova-lvgl-ui` for its screen" if the device has a display.

The agent asks about your device one question at a time, checks the Pi over
SSH (or gives you the exact commands to run), proves the Pi answers with the
step 2 client, and only then writes, builds and verifies the client for your
device.

### Shortcut for the Waveshare ESP32-S3 box: ready-made firmware

For the Waveshare ESP32-S3-Touch-LCD-1.85C there is a ready-made client in
`examples/realtime/esp32/`. It compiles with ESP-IDF v5.4 and fits (about 86%
of the 1 MB app partition), but it has not run on a real board yet, and it is
audio only. To use it, with ESP-IDF v5.4 or its Docker image:

1. Copy the Pi's `cert.pem` over `main/nova_cert.pem`.
2. `idf.py set-target esp32s3 && idf.py menuconfig`:
   - Example Connection Configuration → Wi-Fi SSID and password
   - Nova realtime → URL `wss://<pi-ip>:8767/v1/realtime`, the token, the wake mode
3. Check the pins in `main/board.h` against your board revision (the defaults are V1).
4. `idf.py build flash monitor`. The BOOT button wakes Nova.

To add Nova's screen to this board, use `skills/nova-lvgl-ui`: it keeps the
portable `nova_face.c` and adds the board's display and touch setup. The
screen needs a larger app partition (the board has 16 MB of flash).

## If it does not work

| Symptom | Fix |
|---|---|
| Every answer is "I didn't catch that" | The mic is too quiet: the Pi needs speech RMS of at least 0.016. Raise the mic gain. |
| `deny_bad_token` | Wrong or empty token. Export it in the same shell command that runs the client. |
| `session_limit_reached` | Another client (the Qt HMI, a browser) holds the one session. |
| TLS handshake fails | The Pi's address changed: restart the launcher (new certificate), then copy `cert.pem` to the device again. |
| Nothing answers on :8767 | The server is not running, or it runs with `NOVA_HAILO_TLS=off`. |

Protocol details: `docs/api/realtime.md` (audio, events, errors) and
`docs/api/ui-state.md` (screens).
