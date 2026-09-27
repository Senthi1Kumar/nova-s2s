# Nova Realtime API (`nova-realtime/1`)

Turn any device with a microphone and a speaker into a Nova voice agent. The
device only moves audio; the Raspberry Pi running nova-hailo does everything
else — wake word (optional), voice activity detection, speech-to-text, the LLM
and its tools, and text-to-speech.

```
device mic ──PCM16──► wss://<pi>:8767/v1/realtime ──► [wake] ► VAD ► STT ► LLM+tools ► TTS
device speaker ◄──────────── response.audio.delta ◄──────────────────────────────────┘
```

The event names follow the OpenAI Realtime API. One WebSocket is one
conversation. The Pi serves **one session at a time**, and the Qt HMI counts as one.

## Quick start (LAN)

New device? Follow `docs/api/getting-started.md`, or ask a coding agent to use
`skills/nova-realtime-client`: it asks about your device and does the setup.

On the Pi:

```bash
./scripts/run_demo_oem.sh      # wss on :8767, plain ws on 127.0.0.1:8766
```

The first run makes a TLS certificate (for the Pi's current IP) and an API
token in `~/.config/nova-hailo/` and loads them; later runs reuse them, and a
changed IP gets a new certificate. `NOVA_HAILO_TLS=off` runs plain `ws://`
without a token (lab only).

`run_demo_oem.sh` is the launcher: it selects the config profile
(`NOVA_HAILO_PROFILE`, default `oem`), sets up the microphone and echo
cancellation, logs to `logs/demo/`, and then starts `scripts/run_web.sh`.
Don't run `run_web.sh` directly; it expects the launcher to have set
`NOVA_HAILO_CONFIG`.

From any machine on the LAN, with the Pi's certificate and token:

```bash
scp <pi>:.config/nova-hailo/tls/cert.pem cert.pem
export NOVA_HAILO_API_TOKEN=$(ssh <pi> command cat .config/nova-hailo/api_token) && \
python examples/realtime/python/nova_client.py \
  --url wss://<pi-ip>:8767/v1/realtime --token "$NOVA_HAILO_API_TOKEN" \
  --ca cert.pem --wake server          # then say "Hey Nova, what time is it?"
```

## Connecting

| | |
|---|---|
| URL | `wss://<pi>:8767/v1/realtime` (TLS on) · `ws://<pi>:8766/v1/realtime` (TLS off, lab only) |
| Auth | Header `Authorization: Bearer <NOVA_HAILO_API_TOKEN>`. Browsers, which cannot set WebSocket headers, use `?token=<token>`. Clients on the Pi itself (loopback) need no token. |
| TLS | The Pi's certificate is self-signed. Trust exactly that certificate: embed `cert.pem` (ESP-IDF `cert_pem`) or pass it as the CA file. Do not disable verification: the token travels in the handshake, so the Pi must be verified before it is sent. In a phone browser, compare the SHA-256 the launcher printed when it made the certificate (`openssl x509 -in ~/.config/nova-hailo/tls/cert.pem -noout -fingerprint -sha256` on the Pi) before accepting. |

Without TLS, anyone on the Wi-Fi can read the token and the audio. Phone
browsers also refuse the microphone on plain `http://`.

## Session flow

1. Connect. The server sends `session.created` (check `session.protocol == "nova-realtime/1"`), then `nova.fsm`. The handshake takes about half a second, and `session.created` usually arrives in the same network read as the handshake reply: drain your WebSocket library's buffer before waiting on the socket. Each connection starts a new conversation.
2. Send one `session.update` declaring what your device can do:

```json
{"type": "session.update", "session": {
  "input": {"wake": "server", "echo_cancellation": false},
  "audio": {"output": {"sample_rate": 16000}}
}}
```

| Field | Values | Meaning |
|---|---|---|
| `input.wake` | `"server"` (default when the Pi has "Hey Nova") · `"device"` | Who detects the wake word. |
| `input.echo_cancellation` | `true` · `false` (default) | Whether your mic cancels your own speaker. |
| `audio.output.sample_rate` | 16000 · 22050 · 24000 · 48000 | Rate the Pi resamples speech to. Pin it to your codec's rate. Omitted = native rate, stated on every delta. |

3. Stream microphone audio, play `response.audio.delta`, acknowledge playback.

### Wake mode `server` — "Hey Nova" runs on the Pi

Stream the microphone **all the time, silence included**. The Pi scores every
chunk. Nothing is transcribed until it hears "Hey Nova"; then it sends
`wake.detected` and listens for your question. Best for mains-powered devices
with no wake model of their own.

### Wake mode `device` — your device's own wake word

1. Keep the socket open. While `nova.fsm` state is `IDLE`, **send no audio**.
2. When your local wake word fires (or a button is pressed), send `{"type": "session.arm"}` and start streaming immediately.
3. **Pre-roll:** keep a 300–500 ms ring buffer of mic audio and send it right after `session.arm`. People say "Hi ESP, what's the fuel level" in one breath, so the first words arrive before your wake detector fires.
4. Keep streaming while the state is not `IDLE`. After a reply the Pi stays in `LISTENING` for a follow-up window (`pipeline.session_idle_sec`, default 45 s); when it drops to `IDLE`, stop.

Don't open a new socket per wake. A TLS handshake costs 100–300 ms on a microcontroller.

## Audio format

| Direction | Format | Transport |
|---|---|---|
| Device → Pi | PCM16 little-endian, 16000 Hz, mono | **Binary** WebSocket frame of raw PCM (recommended), or `{"type":"input_audio_buffer.append","audio":"<base64>"}` |
| Pi → Device | PCM16 little-endian, mono, rate in `sample_rate` | `{"type":"response.audio.delta","delta":"<base64>","sample_rate":16000,"generation_id":N}` |

- Send 20–40 ms per frame on a LAN (640–1280 bytes). The audio itself is 32 KB/s; frame size, not bandwidth, sets latency.
- An odd-length binary frame is rejected with `invalid_audio_frame`.
- Each delta carries **at most 200 ms** of audio (6400 bytes of PCM at 16 kHz), so a small receive buffer is enough. Queue decoded deltas for a separate playback task; never block the socket reader on the speaker.
- Start playing on the first delta; keep at most 60–100 ms of jitter buffer.
- Expect about 1.1–1.4 s from the end of the user's speech to the first reply audio (Raspberry Pi 5 + cloud LLM, device on the same LAN).
- If `generation_id` changes mid-playback, or `response.done` arrives with `response.status == "cancelled"`, the old reply was interrupted: flush what you have queued and send `{"type":"playback.interrupted"}` instead of `playback.stopped`.

## Echo and interruption

Your speaker sits next to your microphone, so the Pi has to know when Nova is audible.

- Send `{"type":"playback.started"}` when the first delta starts playing, and `{"type":"playback.stopped"}` when the speaker is really silent again (not when the last delta arrives).
- `echo_cancellation: false` — while you play, the Pi ignores your uplink for turn-taking and wake. Safe, but the user cannot interrupt by voice.
- `echo_cancellation: true` — the Pi keeps listening while you play, so the user can interrupt: say "Hey Nova" (`server` mode), or, in `device` mode, send `{"type":"response.cancel"}` then `{"type":"session.arm"}` when your wake word fires.

## Events

Clients **must ignore event types they do not know.**

| Tier | Device → Pi | Pi → Device |
|---|---|---|
| Required | `session.update` · mic audio · `playback.started` · `playback.stopped` | `session.created` · `response.audio.delta` · `response.done` · `error` |
| Recommended | `session.arm` | `nova.fsm` · `wake.detected` |
| Optional | `response.cancel` · `playback.interrupted` | `conversation.item.input_audio_transcription.completed` · `response.audio_transcript.delta` · `response.audio_transcript.done` · `input_audio_buffer.speech_started` · `input_audio_buffer.speech_stopped` · `nova.turn_metrics` |

`nova.settings.*`, `nova.google.*`, `nova.llm_status`, `nova.tool_status` and
`nova.research_status` belong to the operator UI and are not part of this contract.

`nova.fsm` → `{"type":"nova.fsm","fsm":{"state":"IDLE|ARMED|LISTENING|TRANSCRIBING|THINKING|SPEAKING|INTERRUPTING", ...}}`.
For a screen or LED: `IDLE` sleeping · `ARMED`/`LISTENING` listening ·
`TRANSCRIBING`/`THINKING` thinking · `SPEAKING` speaking.

`wake.detected` → `{"type":"wake.detected","source":"server|device","score":0.42|null,"fsm":{...}}`.

## Errors

`{"type":"error","error":{"type":"<type>","message":"..."}}`

| `error.type` | Cause | Socket |
|---|---|---|
| `deny_bad_token` | wrong token | closed (1008) |
| `deny_no_token_configured` | the Pi has no `NOVA_HAILO_API_TOKEN` set | closed (1008) |
| `session_limit_reached` | another client (e.g. the Qt HMI) holds the session | closed (1008) |
| `invalid_audio_frame` | binary frame with an odd byte count | stays open |
| `unsupported_sample_rate` | `audio.output.sample_rate` not in the list | stays open |
| `invalid_session_update` | bad `input.wake` / `input.echo_cancellation`, or `wake:"server"` on a Pi without a wake model | stays open |

## Reference clients

- `examples/realtime/python/nova_client.py` — laptop "device": WAV in/out, live mic, both wake modes.
- `examples/realtime/esp32/` — ESP-IDF firmware for ESP32-S3 (board pins in `board.h`).
- `examples/realtime/lvgl/` — "Nova face": an LVGL v9 screen driven by the UI-state contract (`docs/api/ui-state.md`); `nova_client.py --ui` runs it.
- `skills/nova-lvgl-ui/` — a skill that builds such a screen for your device.
- `skills/nova-realtime-client/` — a skill that lets a coding agent build a client for your device.

## Not yet (v2)

Access from outside the home network (a secure bridge to the Pi), per-device keys, WebRTC transport.
