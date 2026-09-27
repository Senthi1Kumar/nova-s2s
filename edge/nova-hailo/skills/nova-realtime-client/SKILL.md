---
name: nova-realtime-client
description: Build and connect a client for the Nova realtime voice API (nova-realtime/1) on any device — ESP32/ESP-IDF, Arduino, Raspberry Pi, Linux, desktop or phone. Covers asking about the device, setting up the Pi side (TLS certificate, API token, server launch), verifying the Pi before writing device code, and the client itself. Use when someone wants a microphone+speaker device to talk to a Nova (nova-hailo) Pi, asks how to connect or provision a device, or wants a screen for it (together with nova-lvgl-ui).
---

# Nova realtime client

The device is **audio I/O only**: it streams microphone PCM to the Pi and plays
the PCM that comes back. Never put STT, an LLM or TTS on the device. The full
contract is `references/protocol.md`; read it before writing code.

Work through these steps in order. Do not write device code before step 3 passes.

## 1. Intake

Ask one question at a time; skip anything the user already told you.

1. Device and framework (ESP32-S3 + ESP-IDF, Arduino, Linux/Python, desktop, phone).
2. Microphone and speaker hardware. Do they share one I2S clock? (Then use 16 kHz both ways.)
3. Does the mic path have acoustic echo cancellation?
4. Wake: a button, a local wake word, or none (then the Pi's "Hey Nova").
5. **A screen?** If yes, also use the `nova-lvgl-ui` skill; this client sends it
   UI-state per `references/ui-state.md`.
6. The Pi's address, and whether you (the agent) can SSH to it.
7. Battery or mains? On battery prefer `device` wake: the socket idles silently.

## 2. Provision

Follow `references/provisioning.md`. The Pi's launcher makes its TLS certificate
and API token itself, so provisioning is: find the live checkout, make sure the
server listens with TLS, then give the device the certificate and the token.
With SSH you do it yourself and **ask before restarting a running server**.
Without SSH, give the user the exact commands from that file. Never put the
token in committed code.

## 3. Verify the Pi first

From any laptop with this repo, before any device code:

```bash
export NOVA_HAILO_API_TOKEN=$(ssh <pi> command cat .config/nova-hailo/api_token) && \
python examples/realtime/python/nova_client.py \
  --url wss://<pi>:8767/v1/realtime --token "$NOVA_HAILO_API_TOKEN" --ca cert.pem \
  --wav examples/realtime/python/samples/what-time-is-it.wav --wake device
```

Run it as one command: many agent tools start a new shell per call, and an
empty token fails as `deny_bad_token`. Without SSH, set `NOVA_HAILO_API_TOKEN`
in the same shell from the Pi's token file.

Pass = exit code 0, a `you :` line with the question, a `nova:` line with an answer.
Exit 2 (no reply) or an `[error]` line = a Pi-side problem: fix it (step 2 and the
debugging table) before touching the device.

## 4. Decide

1. **Wake mode.** Button or local wake word → `input.wake: "device"`: idle socket,
   `session.arm` on wake, 300–500 ms pre-roll, stream while `nova.fsm` state ≠ `IDLE`.
   None → `input.wake: "server"`: stream the mic continuously.
2. **Echo.** Declare `echo_cancellation` truthfully; if unsure, `false`.
3. **Output rate.** Pin `audio.output.sample_rate` to the speaker codec's rate
   (16000 if mic and speaker share a clock).
4. **TLS.** `wss://`, verification on, trusting exactly the Pi's `cert.pem`. The
   token goes in the `Authorization: Bearer` header (browsers: `?token=`).

## 5. Build

1. Connect + auth (the handshake takes about half a second), then send the
   `session.update` from step 4. When `session.created` arrives, log it and
   check `protocol == "nova-realtime/1"`. It usually arrives in the same
   network read as the handshake reply: if your WebSocket library buffers
   data (libcurl `CURLOPT_CONNECT_ONLY` + `curl_ws_recv`), drain the library
   until it reports no data before you wait on the socket again, or the first
   events look seconds late.
2. Uplink: 16 kHz mono PCM16LE in 20–40 ms **binary** frames.
3. Downlink: queue `response.audio.delta` for a separate playback task (never block
   the socket reader); play at its `sample_rate`; send `playback.started` on first
   audio and `playback.stopped` when the speaker is really quiet; on a cancelled
   `response.done` or a new `generation_id`, flush and send `playback.interrupted`.
4. Paint `nova.fsm` states; with a screen, send UI-state (`references/ui-state.md`).
5. Ignore unknown event types. Log `error` events.

`examples/realtime/python/nova_client.py` shows every rule above (`--ui` shows the
screen hook); `examples/realtime/esp32/` is the ESP-IDF version.

## 6. Verify the device

`session.created` → wake → `you:` transcript → reply audio plays → `playback.stopped`.
Then check an interruption (with AEC) and a follow-up question.

## Debugging checklist

| Symptom | Likely cause |
|---|---|
| Step 3 fails (exit 2 or `[error]`) | Pi side: server not running with TLS, wrong token, stale cert. Fix before the device. |
| Socket closes right after connect | The `error.type` says why: token (`deny_*`) or another client holds the one session (`session_limit_reached`). An empty `$NOVA_HAILO_API_TOKEN` (exported in another shell) also gives `deny_bad_token`. |
| Never answers (server wake) | Mic not streaming while idle; wrong rate (must be 16000); stereo or 32-bit samples sent as-is. |
| Never answers (device wake) | `session.arm` not sent, or streaming stopped before `IDLE`. |
| "I didn't catch that" every time | Mic too quiet: the Pi's gate needs speech RMS ≥ 0.016; raise the mic gain. |
| First words of the question missing | No pre-roll after the local wake word. |
| Nova interrupts or answers itself | `playback.stopped` sent too early, or `echo_cancellation: true` without real AEC. |
| Reply too fast/slow | Playing at the wrong rate — use the delta's `sample_rate`, or pin it. |
| TLS handshake fails | Cert not embedded, or the Pi's IP changed (it is in the cert) — rerun `make_lan_cert.sh`. |
