# Nova-Hailo HMI WebSocket protocol (showcase subset)

Backend: FastAPI `ws://HOST:8766/v1/realtime`  
Client: browser UI **or** Qt HMI — **one voice session at a time** (`_MAX_SESSIONS = 1`).

## Connect

1. Open WebSocket to `ws_url` from `GET /config` or env `NOVA_HAILO_WS_URL`.
2. Server sends `session.created` then `nova.fsm`.
3. Client sends:

```json
{"type":"session.update","session":{"arm":true,"ptt":true}}
```

## Client → server

| type | payload | notes |
| --- | --- | --- |
| `session.update` | `session.arm` / `session.ptt` | arm for PTT profiles |
| `nova.settings.get` | — | current Local/Cloud LLM catalog |
| `nova.settings.set` | `mode`, `local_hef`, `or_model`, `ns`, `ns_strength`, `gate_min_rms` | LLM switch and/or voice (DTLN, energy gate) |
| `input_audio_buffer.append` | `audio`: base64 **PCM16 LE mono 16 kHz** | ~20–40 ms chunks |
| `response.cancel` | — | barge / stop |
| `playback.started` | `generation_id`, `t_ms` | first audible chunk |
| `playback.interrupted` | `reason`, `t_ms` | local flush after cancel |

## Server → client (HMI must handle)

| type | use |
| --- | --- |
| `session.created` | `session.fsm`, `session.stt_engine` |
| `nova.fsm` | drive UI state (IDLE/LISTENING/…) |
| `input_audio_buffer.speech_started` | listening cue |
| `input_audio_buffer.speech_stopped` | thinking / STT |
| `conversation.item.input_audio_transcription.completed` | user transcript |
| `response.audio_transcript.delta` / `.done` | assistant text |
| `response.audio.delta` | `delta` base64 PCM16, `sample_rate` (often 24000) |
| `response.done` | turn end; stay SPEAKING until local play queue empty |
| `playback.cancel` | flush play queue |
| `nova.research_status` | optional status line |
| `nova.settings` | `mode`, `local_hef`, `or_model`, catalogs, `has_or_key` |
| `nova.llm_status` | `loading` / `ready` / `error` |
| `nova.turn_metrics` | OPS drawer: STT / LLM / TTS / TTFA |

## UI state mapping

Prefer `nova.fsm.state`. Override:

- First `response.audio.delta` → show **SPEAKING** until playback queue drains after `response.done`.
- `playback.cancel` / stop → **INTERRUPTING** then LISTENING.

## Settings (LLM)

Top bar: **NOVA** + Local Hailo / Cloud OpenRouter, FSM in the center, **OPS** / **Chat** / settings on the right. Centered orb like the WebUI. Live YOU/NOVA cards fade; full history is in Chat (user, tool pills, assistant). OPS shows last-turn latencies. Settings: runtime models, noise-gate RMS, DTLN on/off and mix.

## Non-goals (v1)

Google OAuth UI in the orb, dual voice clients, pVAD enrollment, wake-word model.
