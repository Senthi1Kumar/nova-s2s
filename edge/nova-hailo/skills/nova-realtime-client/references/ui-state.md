# Nova UI-state contract (`nova-ui-state/1`)

A Nova client that has a screen drives it with small, independent updates.
The client owns the connection to the Pi (`docs/api/realtime.md`); the UI only
renders. Between two processes (a desktop, the Pi) the updates travel as
**JSON lines** — one object per line, UTF-8. Inside one firmware (an ESP32)
they are **direct function calls**, one per field, with the same meaning, so
the UI code ports unchanged.

## Client → UI

| Field | Values | Source (realtime event) |
|---|---|---|
| `conn` | `"connecting"` · `"up"` · `"down"` | before the handshake · after `session.created` · on close or a fatal error; on `down` the UI shows IDLE |
| `state` | `"IDLE"` · `"LISTENING"` · `"THINKING"` · `"SPEAKING"` | `nova.fsm` → `fsm.state`, simplified (below) |
| `you` | final transcript text | `conversation.item.input_audio_transcription.completed` → `transcript` |
| `nova` | reply text | `response.audio_transcript.done` → `transcript` |
| `level` | number 0.0–1.0 | mic loudness: `min(1.0, rms / 0.1)` over the last 100 ms of samples scaled to −1..1; at most 10 updates per second |
| `error` | error type string; `""` clears it | `error` → `error.type`, e.g. `session_limit_reached`, `deny_bad_token` |

State simplification (the server's FSM has more states than a screen needs):

| `nova.fsm` state | `state` |
|---|---|
| `IDLE` | `IDLE` |
| `ARMED`, `LISTENING`, `INTERRUPTING` | `LISTENING` |
| `TRANSCRIBING`, `THINKING` | `THINKING` |
| `SPEAKING` | `SPEAKING` |

## UI → client

| Field | Meaning | Client action |
|---|---|---|
| `wake` | `true` when the tap-to-talk button is pressed | send `session.arm` — a local wake in `device` wake mode, push-to-talk in `server` mode |

## Rules

- One JSON object per line. Unknown fields are ignored in both directions.
- The UI never slows the client: the client keeps at most 64 pending lines and
  drops the oldest when full; `level` is rate-limited to 10 Hz.
- Every field carries a full value, never a delta, so a UI that starts late or
  restarts is correct again after the next updates.

## Example

```
{"conn":"up"}
{"state":"LISTENING"}
{"level":0.42}
{"you":"What's the weather in Delhi today?"}
{"state":"THINKING"}
{"state":"SPEAKING"}
{"nova":"It's about 33 degrees with a light chance of rain."}
{"state":"IDLE"}
```

## In one firmware

The reference UI (`examples/realtime/lvgl/src/nova_face.h`) exposes one call
per field: `nova_face_set_conn`, `nova_face_set_state`, `nova_face_set_you`,
`nova_face_set_nova`, `nova_face_set_level`, `nova_face_set_error`, and takes
a wake callback in `nova_face_create`. Call them from the realtime event
handler on the LVGL thread.
