# Setup

## Clone and submodules

```bash
git clone <this-repo-url> nova-s2s
cd nova-s2s
git submodule update --init --recursive
```

Submodules:

- `cloned/speech-to-speech` — Hugging Face realtime stack (Nova-adapted); see [THIRD_PARTY.md](THIRD_PARTY.md)
- `Drive_auth_edge` — core payment gate (Parth upstream; see [pipeline.md](pipeline.md#driveauth-two-gates))

## Python

```bash
uv sync
cp .env.example .env
```

Fill every variable you need from `.env.example`. At minimum:

- `LLAMA_SERVER_BIN` — absolute path to a built `llama-server`
- GGUF paths in `nova/launch/models.yaml` for `llm_profile` and `router_llm_profile`

## Build llama-server (do not commit the binary)

```bash
git clone https://github.com/ggerganov/llama.cpp.git /path/to/llama.cpp
cmake -S /path/to/llama.cpp -B /path/to/llama.cpp/build -DGGML_CUDA=ON
cmake --build /path/to/llama.cpp/build -j
echo "LLAMA_SERVER_BIN=/path/to/llama.cpp/build/bin/llama-server" >> .env
```

CPU-only builds omit `-DGGML_CUDA=ON`.

## Run

```bash
uv run python scripts/run_demo.py
```

Opens the tool UI at `http://127.0.0.1:8000/`. This is a process supervisor, not a test.

### STT: SenseVoice (default) vs Audio8 (opt-in A/B)

Default [`nova/config.yaml`](../nova/config.yaml) uses **SenseVoiceSmall** (`stt: sensevoice`). That path stays supported.

To try **Audio8-ASR-0.1B** ([HF](https://huggingface.co/AutoArk-AI/Audio8-ASR-0.1B), CC-BY-NC-4.0 — research/demo only) without changing the default:

```bash
hf download AutoArk-AI/Audio8-ASR-0.1B
NOVA_CONFIG=nova/config.audio8.yaml uv run python scripts/run_demo.py
```

Revert anytime by unsetting `NOVA_CONFIG` (or setting `stt: sensevoice` again). Audio8 is non-streaming full-segment ASR after Silero VAD — same contract as SenseVoice (Audio8 itself has no VAD). `config.audio8.yaml` uses the official HF ASR prompt and `audio8_skip_progressive: true`. If transcripts collapse to `English English…`, you hit a known Audio8-0.1B prompt-regurgitation bug — switch back to SenseVoice (`unset NOVA_CONFIG`).

## Optional keys

| Variable | Tool |
|----------|------|
| `TAVILY_API_KEY` / `BRAVE_API_KEY` / `SERPER_API_KEY` | Web search / research |
| `GOOGLE_CLOUD_PROJECT` / `GOOGLE_OAUTH_CLIENT_ID` / `SECRET` | Calendar, Gmail, Drive (Workspace MCP) |
| `DRIVEAUTH_*` | Payment auth (mock defaults are fine for demos) |

Missing keys degrade the related tool to an explicit unavailable result; they do not crash the voice loop.

Google Cloud Console steps (APIs, OAuth consent, MCP): [google_workspace.md](google_workspace.md).

## Thor single-model (full toolbox)

One GGUF on `:8080`, every registered tool visible (`tool_route_mode: full`), no tool-router on `:8081`.

```bash
# On the board: set LLAMA_SERVER_BIN, ensure models.yaml gguf_path for gemma-4-e2b-it-q4 (or edit config.thor.yaml)
export LLAMA_SERVER_BIN=/absolute/path/to/llama-server
NOVA_CONFIG=nova/config.thor.yaml uv run python scripts/run_demo.py
```

Expect log lines: `Loading config from .../config.thor.yaml`, `route_mode=full`, **one** `llama-server` healthy at `:8080`, and **no** `Starting tool-router llama-server`.

### Smoke checklist

1. Open `http://127.0.0.1:8000/` → Call (or tunnel from the SSH client).
2. Calendar / email (Google OAuth connected) — model should emit a real function call; reply uses tool `speak`, not the tool name alone.
3. Weather for a city — same.
4. Payment to a known beneficiary — DriveAuth mock ACCEPT or STEP_UP as configured; no Trust/Risk on non-payment turns.
5. Watch TTFB / markup: no `<|tool_call_…|>` in TTS; full toolbox increases prefill vs dual-LFM.

Small-GPU dual LFM remains the default via `nova/config.yaml` (`tool_route_mode: model`). See [pipeline.md](pipeline.md).
