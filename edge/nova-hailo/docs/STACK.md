# Runtime stack and profiles

Single reference for what runs by default, which configs exist, and which model files are used.

## Launch

```bash
cd nova-s2s/edge/nova-hailo
./scripts/run_demo_oem.sh
# Driver UI:  cd hmi_qt && ./run.sh
# Web UI:     http://localhost:8766/
```

Default config: `config.oem_v002_test.yaml`  
Env template: `.env.example` → `.env` (never commit secrets)

| `NOVA_HAILO_PROFILE` | Config file | Tools |
| --- | --- | --- |
| `oem` (default) | `config.oem_v002_test.yaml` | web_search, deep_research, check_calendar, check_email, list_drive_files |
| `conversation` | `config.oem.yaml` | off |
| `oem_rollback` | `config.oem_rollback.yaml` | off (short chat only) |

Override any profile with `NOVA_HAILO_CONFIG=/path/to.yaml`.

## Default stack (`oem`)

| Stage | Implementation | Where it runs |
| --- | --- | --- |
| Capture / playback | Qt HMI (`hmi_qt/run.sh`) or browser WS PCM | This Pi (or laptop client) |
| VAD | Silero ONNX | CPU |
| Utterance gate | rms / peak / speech fraction | CPU |
| ASR | NeMo Speech EN streaming GGUF + optional sidecar | CPU |
| Host | Router + controller; compact codec `t0`–`t6` (Phase A) | CPU |
| LLM | Qwen2-1.5B HEF (`llm_backend: cpp` after on-device `.so` build) | Hailo-10H |
| Web search | Exa (summary/highlights) → Brave → Serper | Network |
| Deep research | Tavily async job | Network |
| TTS | Inflect-Nano-v2 ONNX | CPU |

Barge-in while speaking is **off** (UI stop button still interrupts).

## Models and artifacts (paths relative to package root)

| Role | Path / setting |
| --- | --- |
| ASR (default) | `models/nemo_speech/nemotron-speech-streaming-en-0.6b.q8_0.gguf` | `./scripts/fetch_models.sh` (NVIDIA HF) |
| ASR lib | `models/nemo_speech/libnemo_speech_asr_c.so` | `./scripts/fetch_models.sh` builds NeMo-Speech.cpp |
| ASR VAD mask (optional) | `models/nemo_speech/silero-v6.2.0.gguf` | optional |
| ASR rollback | `models/parakeet/tdt_ctc-110m-f16.gguf` | optional |
| ASR NPU fallback | Whisper HEF (`whisper_hef: base`) | hailo-apps |
| LLM | `models/Qwen2-1.5B-Instruct.hef` (`qwen2`) | `./scripts/fetch_models.sh` (Hailo GenAI zoo 5.1.1) |
| TTS | `models/Inflect-Nano-v2-ONNX/` (also `cloned/Inflect-Nano-v2-ONNX`) | `fetch_models.sh` |
| TTS rollback | `models/piper/en_US-amy-low.onnx` | `fetch_models.sh` |
| VAD | `models/silero_vad.onnx` | `fetch_models.sh` |

Sidecar binary (when used):  
`cloned/NeMo-Speech.cpp/build/cpu-server/bin/nemo-speech`  
Server ASR endpointing stays **disabled**; Silero owns turn ends.

## Config knobs (optional)

Under `voice:` in the active YAML:

- Gate: `gate_min_rms`, `gate_min_peak`, `gate_min_sec`, `gate_min_speech_frac`
- ASR wait after speech end: `nemo_wait_floor_s`, `nemo_wait_cap_s`, `nemo_wait_base_s`, `nemo_wait_scale`
- Echo: `echo_tail_ms`, `barge_in_while_speaking`

Under `model:`:

- `stt_engine`, `nemo_speech_model`, `nemo_rnnt_right_context`
- `llm_hef`, `llm_backend`, `tts_engine`, `max_tokens`

Under `tools:`:

- `profile`, `enabled`, `summarize_search` (**false** in this drop), `timeout_sec`
- Native LLM: `scripts/build_hailo_llm_cpp.sh` → `nova_hailo/backends/hailo_llm_cpp*.so` (gitignored)

## Secrets (`.env`)

| Variable | Used for |
| --- | --- |
| `EXA_API_KEY` | Primary web search |
| `BRAVE_API_KEY` / `SERPER_API_KEY` | Search fallbacks |
| `TAVILY_API_KEY` | Deep research |
| `GOOGLE_OAUTH_*` | Calendar / Gmail / Drive (after one-time connect) |

Missing keys: chat still runs; tools fail closed (honest unavailable).

## Quick showcase turns

1. Hello  
2. What can you do?  
3. Stock price of NVIDIA  
4. Current Prime Minister of the UK  
5. Search again  
6. Unread email / files in my drive (if Google connected)  
7. Soft speech / empty → “I didn’t catch that.”  
8. Goodbye  

## Dependencies

See `pyproject.toml`. Live search uses **`httpx`** only (no `exa-py` on the runtime path). Hailo packages come from the system install, not PyPI.
