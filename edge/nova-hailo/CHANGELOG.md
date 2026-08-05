# Changelog

All notable changes to nova-hailo. Measured numbers come from runs on the
target device (Raspberry Pi 5 + Hailo-10H, HailoRT 5.1.1); anything not
measured is called out as such.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/1.1.0/).

---

## [Unreleased] — v0.0.1 demo readiness

### Added — websearch FastMCP tools (built for v0.0.2, gated off for v0.0.1)

- Deterministic router → broker → speak (still **no** LLM JSON tool-calling).
- **`web_search`**: Brave → Serper sync via `nova_hailo/tools/search_mcp.py`.
- **`deep_research`**: Tavily advanced search on a background thread with job
  status `searching` → `generating` → `done`/`failed`; filler TTS then poll.
- **Search summarizer** (`nova_hailo/tools/search_summarizer.py`): after cleaner,
  a dedicated summarizer HEF (`tools.summarizer_hef`, default `qwen2`) or the
  chat HEF summarizes evidence under the GenAI lock → TTS. Chatbot refusals /
  identity collapse are rejected → `speak_fallback`. Numeric literal bypass
  unchanged. Router matches bare/current/latest `news` so asks do not fall
  through to soul.md “no internet” chat.
- Optional FastMCP registration when the `mcp` package is installed; Pi path
  calls the same functions in-process.
- Config at landing time: `tools.profile: websearch_readonly`,
  `enabled: [web_search, deep_research]`. **Reverted for the v0.0.1 demo** —
  current `config.oem.yaml` ships `tools.profile: conversation`,
  `enabled: []`, which keeps this code path built and tested but unreachable
  until v0.0.2. Workspace OAuth deferred.
- WS event `nova.research_status` `{job_id, status}` for orb/debug.
- Env: `TAVILY_API_KEY` (+ existing Brave/Serper). Preflight warns if missing.
- Literature watch: arXiv `2511.03728` in `ROADMAP.md` §6c (load-time Hailo
  `lora_name` exists; adaptive mid-session compression LoRA does not).

### Added — FireRedVAD backend (`NOVA_HAILO_VAD=firered`, A/B against Silero)

- **`nova_hailo/web/firered_vad.py`** (new): `FireRedVadSegmenter`, same
  `feed()`/`reset()`/`set_threshold()` contract as `SileroVadSegmenter`. Pure
  `onnxruntime` + `kaldi_native_fbank` at inference time — no torch/kaldiio
  runtime dependency; those only run once, if ever, to export assets that are
  already staged in `models/firered_vad/`.
- 0.6M-param DFSMN, 10ms causal frames, cache-based streaming
  (arXiv:2603.10420). Published (FLEURS-VAD-102): F1 97.57 / FAR 2.69% vs
  Silero's F1 95.95 / FAR 9.41% — at a third the parameter count.
- **Validated against the reference PyTorch implementation before shipping**:
  798/798 frames, probabilities matching to 3 decimal places, on a real
  recorded utterance. One easy-to-get-backwards detail: FireRedVAD's fbank
  frontend expects int16-amplitude samples (kaldi convention), not Silero's
  `[-1, 1]` floats — feeding the wrong scale silently collapses every
  probability toward zero with no error.
- The turn-taking state machine (pre-roll, min-speech, min-silence,
  max-utterance) is a direct port of `SileroVadSegmenter`'s, so the two
  engines are an A/B on frontend accuracy alone, not on differing endpointing
  tunables.
- **Not a self-barge fix by itself.** FireRedVAD is a *generic* VAD with no
  target-speaker conditioning; Nova's own TTS output is speech, so a lower
  false-alarm rate on noise doesn't stop her hearing herself. See
  `ROADMAP.md` §6a for the full analysis and why FireRedChat's *personalized*
  pVAD (the model that would fix that) is not available to us.
- Measured on the Pi: **1.2 ms per 10 ms frame (RTF 0.12)** — comfortably
  realtime, but ~11× more CPU per second of audio than Silero's RTF 0.011
  (more inference calls at 10ms vs 32ms frames, deeper net). Worth watching
  under concurrent parakeet/LLM/TTS load; not a concern in isolation.
- Tests: `tests/test_firered_vad.py` — a stubbed-probability state-machine
  test (a sine tone alone does not register as speech to this model, unlike
  Silero, so the turn-taking logic is tested independently of the model
  recognising synthetic audio) plus an opt-in real-corpus smoke test
  (`NOVA_FIRERED_TEST_WAV=<path>`). All 3 pass locally and on the Pi against
  real recorded audio.

Scope for v0.0.1 is **natural conversation only**: no tools, no MCP. Web search
and Workspace tools are built and tested but gated off via
`tools.profile: conversation` / `tools.enabled: []` in `config.oem.yaml` until
v0.0.2.

### Fixed — config precedence (root cause of "config.oem.yaml does nothing")

- **`scripts/run_web.sh`** no longer hardcodes `NOVA_HAILO_LLM=qwen2` and
  `NOVA_HAILO_TTS=piper`. Because `web/app.py` lets environment override config,
  these defaults meant `model.llm_hef` in the YAML **was never read** — editing
  it to `qwen25` or `qwen3` silently kept loading Qwen2. This was the single
  cause of three separate "the config is broken" investigations.
- **`scripts/run_demo_oem.sh`** had the same defaults; removed. Both launchers
  now export `NOVA_HAILO_LLM`/`TTS` only when the operator sets them explicitly
  for an A/B, and print `(env override!)` next to any value that is not coming
  from the config file.
- **Launcher banners read the live config** (`llm_hef`, `stt_engine`,
  `tts_engine`, `playback`, `tools.profile`, `barge_in_while_speaking`) instead
  of printing static text, so the banner can no longer disagree with the YAML.
- **`config.py::resolve_llm_hef()` fails closed.** Previously any unrecognised
  alias silently returned the first available HEF — verified: `bogus-name` →
  `Qwen2-1.5B-Instruct.hef`, indistinguishable from "my edit was ignored". It
  now raises `FileNotFoundError` listing the known aliases. Closes TODO P0.10.

Net effect: `config.oem.yaml` is now genuinely the single control point.
Remaining escape hatches are documented under *Configuration* below.

### Changed — ASR migrated from NPU Whisper to CPU parakeet

- **`backends/parakeet_stt.py`** (new): ctypes binding to `libparakeet.so`
  (parakeet.cpp C API, ABI 5), same `transcribe()`/`release()` contract as
  `WhisperSTT`. `pipeline.py::_make_stt()` selects on `model.stt_engine`
  (`parakeet` | `whisper_hef`) and falls back to Whisper if artifacts are
  missing.
- Measured on a **live-recorded real-microphone corpus** (not TTS resynthesis):

  | Engine | WER near | WER far | p50 latency |
  |---|---|---|---|
  | parakeet-tdt_ctc-110m (CPU) | **0.167** | **0.20** | **292 ms** |
  | Whisper-Base (Hailo NPU) | 0.333 | 0.60 | 344 ms |

  parakeet is also the only engine that transcribes the wake word "Nova".
- f16 vs q8_0 near-field: identical transcripts, f16 ~14% faster. Far-field A/B
  still outstanding.
- Frees the NPU for the LLM and keeps ASR off the single Hailo permit.

### Added — device-side context reuse

- `pipeline.native_context` uses HailoRT GenAI's on-device KV cache
  (`get_context_usage_size()` / `max_context_capacity()` / `clear_context()`)
  instead of re-prefilling every turn. **TTFT 946 ms → 316 ms.**
- `_maybe_compact_context()` compacts at `ctx_compact_ratio` (0.7) of the
  compiled 2048-token ceiling. That ceiling is baked into the HEF and is not
  configurable.

### Fixed — audio output and turn-taking

Five passes, several self-inflicted; recording the causes so they are not
re-litigated:

- **No audio from Piper** — synthesis was fine; `voice.playback` was `browser`
  so `local_play=False`, later compounded by a `both` default in the launcher
  that played every clause twice ~1 s apart and re-fed the mic.
- **Mic went dead** — a persistent `sd.OutputStream` was holding the duplex
  `default` ALSA device. Playback now prefers `pulse`/`playback`/`dmixed` and
  never holds `default`.
- **Startup tick** — `_prewarm_playback()` opened the stream without writing;
  writing silence at the wrong rate caused an audible click.
- **Barge-in** abandons playback via chunked 50 ms writes plus
  `abort()`/`start()`, rather than relying on an NPU abort that HailoRT 5.1.1
  does not expose.
- **Self-barge / echo** — the guard now keys off *sent-audio duration* rather
  than `tts.is_speaking`, which only covered the ~250 ms synthesis window and
  left the rest of playback unguarded.
- **"Clause cutoff" was `max_tokens: 32`**, not audio. Added sentence-trim in
  `validate_spoken_unit()`. See *Known issues*.

### Added — grounded web search (built, gated off for v0.0.1)

- **`tools/search_clean.py`** (new). Raw Brave payloads measured at 12.5–90 KB
  (~3.1K–22.4K tokens), up to **11× the entire 2048-token context**. Nearly all
  of it is metadata. `SearchResultCleaner` strips tags, entities, ellipses,
  domains and site names, dedupes, and emits a numbered evidence block capped at
  700 chars (~150 tokens).
- **Numeric queries bypass the LLM entirely.** Measured: given evidence
  containing no price and an explicit "use ONLY these facts" instruction,
  Qwen2-1.5B still produced *"Tesla stock price is currently $304.18"*. Price /
  quote / temperature / distance questions now either quote a number that
  literally appears in the evidence or decline.

### Fixed — deterministic router (`tools/oem_tools.py`)

- `set(enabled or OEM_READONLY)` enabled **all** tools when passed `[]`
  (falsy-default bug). Now `set(OEM_READONLY if enabled is None else enabled)`.
- Greeting regex matched as a *prefix*, so "Hey there can we chat about BMW?"
  answered "Hello. How can I help?". Now anchored to the whole utterance.
- `\bmeeting\b` did not match "meetings" → `meetings?`.
- Added `identity`, `capability`, `smalltalk` intents and `_UNAVAILABLE_CAPS`,
  which decline honestly **only** when the required tool is not enabled, so
  capability declines no longer shadow working tools.
- `.env` was never loaded anywhere; `load_dotenv()` moved to
  `nova_hailo/__init__.py` (placing it in `config.py` did not fire, because
  `oem_tools` never imports `config`).

### Added — benchmarking

- `bench/asr_adapters.py`, `bench/tts_adapters.py`; Pocket TTS and Supertonic
  via ONNX Runtime alongside Piper/Kokoro.
- `scripts/record_asr_corpus.py`: real-mic corpus capture with near/far
  distance, `--redo`, level feedback and 250 ms pre-roll.
- `bench/contracts.py::normalize_text()` now strips punctuation. Previously a
  perfect parakeet transcript scored WER 0.333 purely on punctuation — a
  measurement bug that was hiding the winner.
- `scripts/run_model_matrix.py`: module-level TTS cache (a per-utterance Piper
  instance leaked ONNX sessions and **OOM-froze the Pi**), plus a memory
  preflight.
- Report moved to Typst (`docs/benchmarks/model-matrix-report.typ`) with
  winner-cell highlighting and per-table metric legends.

### Added — Inflect-Nano-v2 TTS backend (`model.tts_engine: inflect`)

- **`backends/inflect_tts.py`** (new): `InflectTTS(QueuedOnnxTTS)` on the
  torch-free ONNX runtime (`duration.onnx` 3.5 MB + `decode.onnx` 12 MB).
  Upstream returns `(sample_rate, waveform)`; the tuple is swapped to match our
  contract. Output is 24 kHz mono float32 **in memory** — no temp-file round
  trip. Deps: `phonemizer`, `espeakng-loader`, `num2words`, `Unidecode`; no
  torch, and espeak-ng is already present for Piper.
- Config: `inflect_model_dir`, `inflect_speed`, `inflect_variation`,
  `inflect_seed` (fixed seed by default so prosody is reproducible).
- **Measured on the Pi**, warm, 4 cores:

  | Utterance | Audio | Synthesis | RTF |
  |---|---|---|---|
  | "Traffic on the A9 is clear." | 1.95 s | 351 ms | **0.180** |
  | "Your next meeting starts at ten past three in Munich." | 3.10 s | 556 ms | **0.179** |

  Init 0.72 s. Comfortably realtime, ~2× the published 0.0933 RTF (measured
  there on an 8 vCPU instance), and ~1.5–2× slower than Piper's ~250 ms — the
  cost of the claimed quality gain. Perceptual A/B against Piper still pending.
- Chosen because it is **VITS-family, one-shot** like Piper, so cost scales
  linearly with utterance length rather than running a token loop.
- **Audio8-TTS-Preview-0.6B-ONNX-INT4 not attempted**: 601M-parameter DualAR
  autoregressive, ~1.1–1.2 GiB at synthesis peak, benchmarked on Apple M2, and
  **publishes no RTF** — the same profile as MOSS below.

### Removed — MOSS-TTS-Nano backend (evaluated, rejected)

- **`backends/moss_tts.py`** (new): `MossTTS(QueuedOnnxTTS)` behind the same
  `synthesize(text) -> (np.ndarray, int)` contract; handles 48 kHz stereo →
  mono downmix and the file-path return, with `enable_wetext=False` to keep
  pynini out of the dependency graph.
- Includes a soundfile-backed **torch/torchaudio stub**. Upstream imports torch
  at module scope but uses it in exactly one place (loading the voice-clone
  reference WAV at init), so the stub avoids a ~300 MB torch install on the Pi.
- Selectable via `model.tts_engine: moss`, falling back to Piper like Kokoro.
- **Measured on the Pi and rejected.** One short sentence
  ("Traffic on the A9 is clear."):

  | | MOSS-TTS-Nano | Piper |
  |---|---|---|
  | Time | **> 6 min, never completed** | ~250 ms |
  | CPU | 384% (all 4 cores pinned) | modest |
  | RSS | 1.75 GB | ~100 MB |

  The autoregressive decode is orders of magnitude away from the < 0.3 RTF a
  realtime turn needs; the upstream "4-core CPU" claim is measured on an M4.
  Code is retained but inert (default remains `piper`).
- **MOSS-Transcribe-Diarize rejected on inspection**: 0.9B, GPU-required,
  batch-only. Cannot run on this hardware.

### Documentation

- `README.md` rewritten with a mermaid architecture diagram (three colour-coded
  zones, dotted echo edge) and corrected component list.

---

## Configuration

`config.oem.yaml` is the control point. `./scripts/run_demo_oem.sh` selects it
via `NOVA_HAILO_PROFILE=oem` → `NOVA_HAILO_CONFIG`. Launching `main.py` or
uvicorn directly instead falls back to `config.yaml`, which carries different
values — prefer the launcher.

Environment variables still take precedence over the file when explicitly set:

| Variable | Overrides |
|---|---|
| `NOVA_HAILO_CONFIG` | which config file loads |
| `NOVA_HAILO_PROFILE` | `oem` \| `oem_rollback` |
| `NOVA_HAILO_LLM` | `model.llm_hef` |
| `NOVA_HAILO_TTS` | `model.tts_engine` |
| `NOVA_HAILO_WHISPER` | whisper HEF |
| `NOVA_HAILO_PLAYBACK` | `voice.playback` |
| `NOVA_HAILO_SEQUENTIAL_STT` | `pipeline.sequential_stt` |
| `NOVA_HAILO_ONE_SENTENCE` | `pipeline.voice_one_sentence` |
| `NOVA_PARAKEET_MODEL` / `_LIB` | parakeet artifacts |
| `NOVA_MOSS_MODEL_DIR` / `NOVA_MOSS_REPO` | MOSS assets |

LLM aliases: `qwen2`, `qwen`, `qwen25`, `qwen2.5`, `qwen3`, `llama32`,
`llama3.2`, `llama`, `qwen2-fc`, `function`, `deepseek`, `r1`, `deepseek-r1`,
`coder`, `qwen-coder`. Anything else now raises rather than silently
substituting.

---

## Known issues

- **Audible TTFA is not aggregated.** `speech_end_to_audible_ms` is wired
  end-to-end (`metrics.py:147`, populated in `web/realtime_session.py` from the
  browser `playback.started` ack) but never rolled into a p50/p95, so the
  headline latency gate is still unsubstantiated.
- **`max_tokens: 32`** hard-stops replies after roughly one short sentence at
  ~7 tok/s. A likely contributor to reported "cuts off mid-conversation".
- **Reasoning ceiling.** INT4 1.5B models get arithmetic wrong (60 km at
  45 min → "120"/"15"; correct is 80) and assert falsehoods from memory
  ("Tesla makes the Taycan"). Reserve the LLM for constrained generation.
- **`logs/demo/latest.log` goes stale** when the app is started outside
  `run_demo_oem.sh`; caused two wrong diagnoses.
- **Echo / barge-in tradeoff** unresolved pending pVAD or hardware AEC.
  Chromium's `echoCancellation` only cancels audio Chromium itself plays.
- **UI**: assistant responses missing from the message sidebar; no tool-call
  colouring.
- ~~Docstring headers (`Callers:` / `User:` / `API:`) still to be stripped
  project-wide.~~ Done — internal conversation references stripped from
  docstrings/comments project-wide; `Callers:`/`API:` headers themselves kept
  (useful, not a leak).

---

## Planned

### Before the demo (2026-08-03)

1. **Aggregate `speech_end_to_audible_ms` into p50/p95.** Small change; every
   other latency decision is guesswork without it.
2. **Instrument one cutoff end to end** — synthesized sample count vs. bytes
   sent over WS vs. browser `playback.started`/`stopped` — to localise the loss
   to synthesis, transport or playback. Revisit `max_tokens` in the same pass.
3. **Acknowledge-then-work filler speech.** Pre-synthesize short Piper clips at
   startup and play one the moment a turn falls through to the LLM. This is the
   standard mask for a 3–4.9 s LLM path and the only Monday-relevant item from
   the harness-engineering review that is not already shipped. No NPU changes.
4. **Benchmark Qwen2.5 against Qwen2** on the contract suite before switching.
   Qwen2 won the original matrix; Qwen3 measured 42% slower.

### v0.0.2 — tools return

- Re-enable web search behind the cleaner and numeric bypass; extend to the
  Workspace read tools.
- Router synonyms (`note`/`document` → Drive); stop `search` matching
  "deep research".
- Speak-fidelity: speak the `speak` field verbatim so the model cannot invent
  detail ("two AM").
- Evaluate **Finnhub** as a real numeric-fact source, replacing regex number
  extraction for price/quote queries.

### Post-demo architecture

Both the 2048-token ceiling and the small-model tool-calling literature
(TinyAgent, Octopus v2, BFCL ~48–57% for Qwen2.5-1.5B) point to the same split
design, so this is the direction rather than a general MCP harness — a single
MCP server's schemas exceed the entire context window:

- **CPU intent classifier / semantic router in front of the LLM** (SetFit or
  MiniLM, ~5–10 ms) to keep the NPU off the critical path for the deterministic
  majority of turns.
- **Tool-RAG**: retrieve ≤ 4 tool schemas per LLM turn; never load the full set.
- **CPU-side structured-output validation.** HailoRT GenAI exposes no logit
  hook, so grammar-constrained decoding is unavailable on the frozen 5.1.1
  stack — enforce with regex + validation + one retry instead.
- Far-field f16 vs q8_0 A/B for parakeet.
- Deferred: FireRedChat pVAD, pipecat smart-turn v3, DriveAuth, CAN/OBD.
- Revisit the HailoRT freeze — abort API, logit access and a larger compiled
  context would each relax a constraint that currently shapes the architecture.
