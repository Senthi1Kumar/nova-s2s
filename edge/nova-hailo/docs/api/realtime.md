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
| Optional | `response.cancel` · `playback.interrupted` | `conversation.item.input_audio_transcription.completed` · `response.audio_transcript.delta` · `response.audio_transcript.done` · `input_audio_buffer.speech_started` · `input_audio_buffer.speech_stopped` · `nova.turn_metrics` · `output_audio.duck` |

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

## Vehicle events (CabinGym)

Sent only when the backend has the `vehicle` tool enabled. Clients that do
not know these types ignore them.

`nova.vehicle` — vehicle state. The first one after `session.created` has
`seq: 0` and a full `snapshot`; later ones carry only `changed` paths.

```json
{"type": "nova.vehicle", "seq": 0, "profile": "ev_demo", "adapter": "sim",
 "snapshot": {"Vehicle.Speed": {"value": 0.0, "source": "sim"}}}
{"type": "nova.vehicle", "seq": 12, "changed": {"Vehicle.Cabin.Sunroof.Position": 100}}
```

`source` is `sim`, `obd`, `gps` or `kuksa`. A value whose source is `sim`
while `adapter` is not `sim` is simulated and must be labelled as such.

`nova.agent_trace` — how one turn's vehicle request was routed and gated.

```json
{"type": "nova.agent_trace", "generation_id": 17, "router": "jev",
 "latency_ms": {"stage1": 118, "stage2": 131},
 "stage1": {"hvac": 0.94, "phone": 0.91},
 "calls": [{"device": "hvac", "api": "set_temperature", "args": {"zone": "driver", "delta": 1},
            "grounding": {"zone": "enum", "delta": "parser"}, "status": "success",
            "gate": {"verdict": "execute", "reason": null}}],
 "gate": ["execute"],
 "diff": [{"path": "Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature", "before": 20.0, "after": 21.0}],
 "speak": "Driver side set to 21 degrees."}
```

`router` is `jev`, `regex`, `cloud` or `backend`; `gate` items are `execute`,
`confirm_first`, `refuse`, `clarify`, `decline`, `cancelled` or `error`.

Delegated turns (multi-step requests handed to the backend agent) produce two
`nova.agent_trace` events with `router: "backend"`:

1. When the turn is delegated, in that turn's generation: one call with
   `device: "delegate"`, `api: "plan"`, `args: {"objective": "<what the user asked>"}`
   and a status/verdict of `accepted`, `offline` (no backend reachable) or
   `busy` (another plan is still running); `diff` is empty and `speak` is what
   Nova said ("On it. I'll tell you when that's done.").
2. When the result is spoken, in the generation that speaks it: the calls the
   backend actually ran (same row shape as a direct turn), their verdicts, the
   combined `diff`, and `speak` = the spoken result. If the backend stopped for a
   confirmation, `gate` ends with `confirm_first` and `speak` is the question.

## Read-only observer (`/v1/observe`)

A display that only shows what is happening (the LVGL cabin UI, a dashboard)
connects to `/v1/observe` instead of `/v1/realtime`. It takes no session lease,
so it runs alongside the HMI that owns the microphone and speaker, and it
sends nothing: the server ignores anything an observer writes. Same token
check as `/v1/realtime` (`?token=` or `Authorization: Bearer`).

The stream starts with `{"type": "observe.hello", "protocol": "nova-observe/1",
"session_active": false}`, the latest `nova.fsm`, a full `nova.vehicle` snapshot
(`seq: 0`) when the vehicle tool is enabled and the latest `nova.nav` /
`nova.agent_trace`, then live events: `observe.session {"active": true|false}`
when a voice session starts or ends, every `nova.*` event of the active session, `session.armed`, the
`input_audio_buffer.speech_*` events, user and Nova transcripts
(`conversation.item.input_audio_transcription.*`,
`response.audio_transcript.*`) and `response.done`. Vehicle changes arrive as
`nova.vehicle` deltas even when no voice session is connected; their `seq`
counts from 1 per server start. Audio is never mirrored. An observer that
falls behind loses its oldest events rather than slowing the voice session.

An online caller (the LiveKit door, e.g. nova-connect in a browser) is mirrored
the same way, with `session_id: "livekit"`: `observe.session`, `nova.fsm` (state
only: `IDLE` while the wake gate is closed, `LISTENING`, `THINKING` from the
final transcript until the pipeline turn ends, `SPEAKING` while Nova's audio
plays), the final user transcript
(`conversation.item.input_audio_transcription.completed`),
`response.audio_transcript.done`, `nova.agent_trace`, `nova.card`,
`nova.tool_status` and `nova.turn_metrics`. Its `generation_id`s are its own,
counting from 1 per caller; there are no transcript deltas.

## Cards (`nova.card`)

A card is what a display shows next to the spoken reply: short lines a driver
can glance at, plus items when there is a list. Sent in the turn's generation
right after `response.audio_transcript.done`, and when a background result
(research, a delegated plan, an announcement) is spoken. Plain car actions get
no card; their `nova.agent_trace` already says what changed.

```json
{"type": "nova.card", "generation_id": 21, "kind": "answer",
 "title": "who won the match yesterday?", "lines": ["India won by 6 wickets."], "items": []}
{"type": "nova.card", "generation_id": 22, "kind": "results", "title": "Nearby",
 "lines": ["Hilltop Cafe is closest."], "items": [{"title": "Hilltop Cafe", "sub": "coffee"}]}
```

`kind`: `answer` (web search, calendar/email/Drive, research, a delegated
plan: title `Planning`, then `Done` or `Needs your answer` with one item per
call `{"title": "lights · set_hazards", "sub": "success"}`, or an announcement:
title `Reminder`), `route` (navigation; title = destination), `results`
(places found), `list` (media). At most four `lines`.

## Navigation (`nova.nav`) and the simulated drive

With `cabingym.sim_drive.enabled`, the server drives a simulated car along real
road geometry when navigation is active, streaming `Vehicle.Speed`,
`Vehicle.Powertrain.Transmission.SelectedGear`, `Vehicle.CurrentLocation.{Latitude,Longitude,Heading}`,
`Vehicle.TraveledDistance` and charge (`…StateOfCharge.Displayed`, `Vehicle.Powertrain.Range`)
at 5 Hz as ordinary `nova.vehicle` deltas, plus the remaining
`…Nav.DistanceKm` / `…Nav.EtaMin` once a second. Source is `sim`; a real GPS
or OBD feeder overrides it (`source` `gps` / `obd`). Parked means no deltas.

`nova.nav` (on `/v1/observe`, and replayed to an observer that joins late)
describes the route whenever navigation starts, changes or ends:

```json
{"type": "nova.nav", "active": true,
 "destination": {"name": "work", "lat": 37.4847, "lon": -122.1477},
 "route": "<encoded polyline, precision 6>", "geometry": "osm",
 "eta_min": 7.6, "distance_km": 6.33,
 "pois": [{"n": 1, "name": "Hilltop Cafe", "lat": 37.443, "lon": -122.158,
           "category": "coffee", "distance_m": 420, "eta_min": 0.5}]}
```

`geometry` is `osm` (baked road geometry), `mapbox` (live router, needs `MAPBOX_API_KEY`) / `osrm` (live router, no key) or
`straight` (no route service reachable). When navigation ends, `active` is
false and `route` is empty.

### Real GPS

With `cabingym.gps.enabled` (`device`, default `/dev/ttyUSB0`; `baud`, default
9600), the server reads an NMEA receiver (RMC, GGA, VTG). Three signals describe it:

| Path | Type | Meaning |
|---|---|---|
| `Vehicle.Private.Nova.Location.GpsFix` | bool | the receiver has a fix (RMC `A` or GGA quality ≥ 1) |
| `Vehicle.Private.Nova.Location.Satellites` | int | satellites in use (GGA) |
| `Vehicle.Private.Nova.Location.Place` | string | area name, e.g. `"Indiranagar, Bengaluru"`; `"Palo Alto"` (the simulated area) until a fix |

With a fix and the car parked, `Vehicle.CurrentLocation.{Latitude,Longitude,Heading}`
and `Vehicle.Speed` come from the receiver (`source` `gps`) and the simulated
drive cannot overwrite them. While the simulated drive plays a route it owns
position and speed (`source` `sim`), so the map puck and the speedometer stay in
step; the route starts at the real position, and on arrival the next sentence
(within a second) brings the real position back. `Place` is reverse-geocoded
with Mapbox (`MAPBOX_API_KEY`) at most once per 60 s and only after moving
500 m; a failed lookup keeps the last name. When the fix is lost (indoors,
unplugged, 3 s of silence), `GpsFix` turns false, the position keeps its last
value and goes back to `source` `sim`, and the simulated drive continues from
there. Deltas carry values only, so a display shows a GPS/SIM chip from
`GpsFix`, not from per-path sources.

Saved places default to Palo Alto. A site elsewhere is set in config:

```yaml
cabingym:
  area: Bengaluru            # Place until the first fix
  places:                    # [lat, lon] or {lat, lon}; overrides by name
    start: [12.9716, 77.5946]  # where the simulated car begins (not a saved place)
    home: [12.9784, 77.6408]
    work: [12.9352, 77.6245]
```

These feed `…Nav.SavedPlaces` ("take me home"), the autoplay route and
"where am I". With a fix, "where am I" answers with `Place`.

## Touchscreen writes (`POST /v1/vehicle/set`)

A display changes the car the same way a voice command does: through the
device API and its safety gate. Same transport check as `/v1/realtime`
(loopback needs no token).

```
POST /v1/vehicle/set   {"path": "Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature", "value": 19}
                       {"alias": "hvac.driver.temp", "value": 19}
200  {"ok": true, "status": "success", "speak": "Driver side set to 19 degrees.", "trace": {...}, "seq": 412}
200  {"ok": false, "error": "refused", ...}
```

Always HTTP 200; check `ok`. `error` is one of `missing_path`, `missing_value`,
`unknown_path`, `not_writable` (a sensor), `unsupported` (no touch control for
that actuator), `bad_value`, `refused` (safety gate), `needs_confirmation`,
`vehicle_disabled`, `bad_json`. `seq` is the `nova.vehicle` seq after the
write; the change itself arrives as a normal delta. Writable: driver/passenger
temperature, fan, seat heating, the four windows, door locks, sunroof,
sunshade, volume.

Event policy: a touch is silent context (the backend planner sees the recent
touchscreen events). Setting the driver temperature to 19 °C or below is
announced at the next quiet moment ("…that's quite cool. Want me to turn on
your seat heating?"), and a spoken "yes" turns the driver seat heating on.

## LLM settings (`GET` / `POST /v1/settings`)

Which language model answers, for a display's settings panel. Same transport
auth as every HTTP route (loopback, or the token from elsewhere).

`GET /v1/settings`:

```json
{"llm": {"provider": "openrouter", "model": "openai/gpt-oss-120b",
         "failover": true, "failover_ready": true,
         "providers": [
           {"id": "openrouter", "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "..."], "ok": true},
           {"id": "groq", "models": ["openai/gpt-oss-120b", "..."], "ok": true},
           {"id": "cerebras", "models": ["gpt-oss-120b", "..."], "ok": false},
           {"id": "local", "models": ["qwen2", "..."], "ok": true}]}}
```

`provider` is the chosen one; `ok` says whether it can be picked (an API key
for a cloud, the Hailo NPU for `local`). `failover` is the switch;
`failover_ready` is true when a second cloud provider actually stands by (its
key is set): on a rate limit (429), a 5xx, a timeout or a dropped
connection, the turn is answered by the other one (groq ⇄ openrouter) instead of
failing, as long as nothing was spoken yet, and the failed provider is skipped
for 60 s. Such a turn's `nova.turn_metrics` has `llm.failover`, e.g.
`"groq→openrouter (429)"`; `provider` does not change.

`POST /v1/settings` with `{"llm": {"provider": "groq", "model": "openai/gpt-oss-120b", "failover": true}}`
(`model` and `failover` optional; for `local`, `model` is a HEF alias) always answers 200:
`{"ok": true, "settings": {"llm": {...}}}`, or
`{"ok": false, "error": "unknown_provider" | "missing_llm" | "<reason>", "settings": {...}}`
with the LLM unchanged. A switch to `local` loads a HEF and takes seconds. The
choice is saved and survives a restart (it wins over `llm.cloud_provider` and
`llm.failover`).

Observers get `{"type": "nova.settings", "llm": {...}}` on `/v1/observe` at
start-up and after every change, and the latest is replayed to one that joins
late, also when the switch came from a nova-connect page over LiveKit. A
`/v1/realtime` client's `nova.settings` carries the same `llm` object next to
the older flat fields.

## Mic mute, wake and cancel (`/v1/session`)

For a display's mic, orb and × buttons. Same transport auth as every HTTP route;
every call answers 200, with `ok: false` and an `error` when refused.

| Call | Body | Answer |
|---|---|---|
| `GET /v1/session` | | `{"muted": false, "source": null, "state": "LISTENING"}` |
| `POST /v1/session/mute` | `{"muted": true, "source": "cockpit"}` | `{"ok": true, "muted": true}` |
| `POST /v1/session/arm` | `{}` | `{"ok": true}`, or `{"ok": false, "error": "mic_muted" \| "no_session"}` |
| `POST /v1/session/cancel` | `{}` | `{"ok": true}` |

- **Mute** turns the microphone off and nothing else: what Nova is saying finishes. While
  muted, no audio reaches the wake word, the VAD or speech recognition, and what the user
  was half-way through saying is dropped. Mute belongs to the cabin, not to one
  connection: it survives a client reconnecting and resets when the server restarts.
- **Arm** opens the mic for a turn as the wake word would (`wake.detected` with
  `source: "cockpit"`), interrupting Nova if she is speaking.
- **Cancel** (×) stops the reply (`response.done` cancelled, reason `ui_cancel`, and
  `playback.cancel`) and puts the session in `IDLE`.
- `state` is the voice session's FSM state, or `IDLE` when no session is connected.

`{"type": "nova.mic", "muted": true, "source": "cockpit"}` goes to `/v1/observe` on every
change and is replayed to an observer that joins later. A `/v1/realtime` client receives
it after `session.created` and on every change, and should stop streaming its microphone
while muted. A client can mute the cabin itself with `session.update`
`{"input": {"muted": true}}` (source `client`). While muted, `session.arm`, `ptt.down` and
`input_audio_buffer.arm` answer `error` with `type: "mic_muted"`.

An online caller (LiveKit) follows the same mute: speech input pauses while
muted, a wake word or tap is ignored until unmute, and the page gets `nova.mic` on
its data channel. The page can mute the cabin itself with
`{"type": "nova.mic.set", "muted": true}` (source `livekit`; a non-boolean answers
`error` `bad_muted`). Cancel aborts the reply and closes the wake window.

`{"type": "output_audio.duck", "gain": 0.3}` asks the device to play Nova's reply at that gain
(0..1) from the next audio frame on, without dropping queued audio; `gain: 1.0` restores it.
The Pi sends it when the driver starts talking over Nova and it is not yet clear whether that
was "mm-hm" (volume back, she carries on) or an interruption (a cancelled `response.done`
follows). A device should also reset to 1.0 on any interruption. Sent only with
`NOVA_HAILO_XVF_TURNS=on` and a trusted echo canceller; a device that ignores it keeps today's
behaviour.

### Nova's own voice

The speaker reaches the microphone. Besides the mic gate while Nova is audible
(plus `voice.echo_tail_ms`), the server keeps these defences (config `voice.*`):

| Setting | Default | Effect |
|---|---|---|
| `trust_client_aec` (env `NOVA_HAILO_TRUST_CLIENT_AEC`) | `false` | A client's `input.echo_cancellation: true` opens the mic during playback only when this is set, or when `NOVA_HAILO_HW_AEC` names a board (the XVF3800 launcher sets `xvf3800`). `false` from the client still closes it. |
| `echo_suspect_s` | `1.5` | Speech that starts while Nova is audible, within this long after, or right after a barge-in is an echo suspect. |
| `fail_closed_cooldown_s` | `6` | The "could you say that again?" line is spoken at most once per window, and never for a suspect. |
| `dedupe_user_s` | `8` | The same request again within this window after it was answered is dropped. |

A transcript whose word pairs mostly (≥ 60 %) come from what Nova said in the last
20 s (her last 8 clauses, interrupted ones included) is dropped as echo
(`response.done` skipped, reason `tts_echo`; a repeat: reason `duplicate`). A suspect
turn never leaves an "unanswered question" for the next prompt, and that question
is offered once only, within `context.unanswered_ttl_s` (60 s).
