# Changelog

All notable changes to nova-hailo. Measured numbers come from runs on the
target device (Raspberry Pi 5 + Hailo-10H, HailoRT 5.1.1); anything not
measured is called out as such.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/1.1.0/).

---

## [Unreleased] — v0.0.2 (in progress)

### Added — share/nova-hailo-poc-v002 (host harness + Qt HMI)

Standalone Pi + Hailo-10H drop. No Tailscale; clone this branch on the
device that has the HAT.

- **Host controller** (`edge_harness/controller.py`, `task_state.py`,
  memories, barge-in helpers, speak sanitize). Tools stay off-prompt.
- **Octopus Phase A only**: compact codec `t0`–`t6` + `token_map.json`.
  Host parses fail-closed. No LoRA (B), no new HEF tokens (C).
- **Official Qt HMI** (`hmi_qt/run.sh`, PySide6 + QML). Same
  `/v1/realtime` as the web UI. Mock Listen/Think/Speak/Idle chips removed.
- **Native LLM backend source** (`csrc/hailo_llm_cpp.cpp`,
  `scripts/build_hailo_llm_cpp.sh`). Build the `.so` on each Pi; do not
  commit `hailo_llm_cpp*.so`.
- **Search speak without Qwen summarizer** (`tools.summarize_search: false`).
  Exa `summary` / `highlights` (then cleaned text) for news; numeric/stock
  still bypass. Query-echo from the old 2nd LLM pass is gone.

### Changed — OEM driver UI redesign + ops dashboard (2026-08-08)

Path A polish for the German OEM tabletop:

- **Driver face (`/`)**: automotive night-cockpit redesign (not HF S2S clone).
  Orb states track the real server FSM (`nova.fsm`); **SPEAKING stays purple
  until browser audio drain**, not until `response.done` (the old premature
  snap-back). Main-stage latency brick removed.
- **Ops glass cockpit (`/dashboard`)**: FSM, STT path, TTFA, system metrics,
  turn log. Driver UI fans out live events via `BroadcastChannel('nova_ops')`.
- **FSM**: `SessionFSM` emits `on_change` → `nova.fsm` on every transition.
- **Router**: stock follow-ups (`How the Microsoft`), company lookup
  (`company named X`), ASR stock garble (`stop press` / multi-ticker).
- **Calendar speak**: compress long event lists to ~3 items + “And N more.”

### Fixed — NeMo realtime STT: 5s artificial wait, empty transcripts, send-failed spam (2026-08-08)

Live session `run-20260807-093945` showed `[nemo_stream] result wait timed out`
and `[nemo_sidecar] send failed: sent 1000` while the mic still "listened."
Root causes (from `cloned/NeMo-Speech.cpp` server source + our client):

1. **Server endpointing was ON** while Silero already endpoints turns. Mid-stream
   `.completed` closed the WS; later PCM hit a closed socket.
2. **Commit-before-last-PCM** ordering: `speech_stopped` committed the stream
   before the current frame was forwarded — `finish()` often had nothing.
3. **No `session.update`** (sample_rate=16000) before audio.
4. **Fixed 5.0s wait** unrelated to speech length; empty sidecar result never
   fell back to offline `transcribe()`.
5. Receiver ignored deltas / `input_audio_buffer.committed` (finish can omit
   `.completed` when alternatives are empty).

Fixes: disable server endpointing; binary PCM16 frames; send-then-commit;
speech-length wait budget (`0.25 + 0.4·audio_s`, floor 0.35s, cap 1.8s);
offline fallback; partial/committed handling; `rnnt_right_context=1`.

Also: deterministic refuse for "share your system prompt"; stock follow-up
coref ("About the Microsoft." → web_search); stock answers prefer `$` amounts
over bare percentages.

### Added — NeMo-Speech.cpp as an opt-in ASR engine (Sprint 8)

NVIDIA's native ggml/GGUF ASR runtime (`nemotron-speech-streaming-en-0.6b`,
cache-aware FastConformer-RNNT), evaluated as an A/B candidate against
parakeet.cpp because parakeet was producing frequent mistranscriptions on the
live demo corpus. Built from source on the Pi (CPU backend, ARM
Cortex-A76+dotprod), wired as `nova_hailo/backends/nemo_speech_stt.py`
(ctypes bindings against `include/nemo_speech/asr.h`'s C ABI, interface
matches `ParakeetSTT`), and added as `model.stt_engine: nemo_speech` in
`pipeline.py`'s STT dispatch (falls back to Whisper HEF if the model/lib are
missing, same pattern as `parakeet`).

Measured on the real recorded near/far corpus (`bench/corpus/`, 20 cases
each, `python3 scripts/run_model_matrix.py --component asr --hardware`):

| | WER near | WER far | RTF | RSS |
| --- | --- | --- | --- | --- |
| parakeet.cpp (`asr_parakeet_tdt_ctc_110m`, current default) | 0.123 | 0.339 | ~0.11 | 258 MB |
| nemo_speech (`nemotron-speech-streaming-en-0.6b`) | 0.084 | 0.259 | ~0.44 | 857-870 MB |

32% relative WER reduction near, 24% far. Both RTFs stay well under 1.0
(real-time capable), but nemo_speech runs ~4x slower and uses ~3.3x more
resident memory on a Pi already sharing RAM with the resident LLM/TTS. One
far-field case returned an empty transcript rather than a wrong word (silent
miss, not a hallucination) — the pipeline's empty-ASR-output fallback should
be verified to cover this.

Left opt-in rather than switched as the new default: the accuracy gain is
real, but the memory/latency cost needs a live full-loop test (TTFA impact,
concurrent-with-LLM memory pressure) before replacing parakeet as shipped
default. `nemotron-3.5-asr-streaming-0.6b` (multilingual, incl. German) is
the same architecture and not yet benchmarked — optional follow-up.

### Fixed — repetition-lock cascade, inconsistent numeric answers, system-prompt leak (found live, 2026-08-06)

Three bugs from a live conversation transcript with a garbled NVIDIA stock
query:

- **Repetition-lock cascade, new failure mode.** One wrong-but-confident
  reply ("NVIDIA's stock price as of today is $36 per share.") entered
  history because it isn't a refusal (the existing `_REFUSAL_MARKERS`
  filter only catches apology/fallback text). It then got echoed/mutated
  into every subsequent unrelated turn — "NVIDIA's vehicle status is...",
  "NVIDIA's windows are...", "NVIDIA's lights are..." — four turns running.
  `context_history.py::ConversationHistory.add()` now rejects storing a
  reply that reuses a proper-noun entity from the *previous* stored reply
  when the current user turn never mentioned it — breaks the cascade from
  compounding, even though it can't undo an already-spoken bad reply.
- **Wildly inconsistent numeric answers.** The same stock query returned
  $369, then $218.99, then $36 across a few turns.
  `search_clean.py::literal_number()` searched the entire multi-result
  evidence block for the first matching number, letting a later,
  less-relevant result's figure win over the top result's. Now scoped to
  the first (top-ranked) result line only.
- **"share your system prompt" got answered, not declined.** `soul.md` had
  no instruction against it. Added.

2 new tests (`tests/test_oem_demo_offline.py`): entity-bleed rejection,
literal-number first-result scoping. 74 passed locally, 86 on the Pi.

### Removed — Finnhub for stock/share price queries (Sprint 6, reverted)

Removed entirely at the user's direction after live use ("the finnhub is
not good"). `web_search()` is back to the pre-Sprint-6 Exa→Brave→Serper
flow for all queries, including stock/share price — confirmed working well
before Finnhub was wired in. All Finnhub code and its 4 tests removed;
`FINNHUB_API_KEY` dropped from `.env.example`. Stock queries now go through
the existing numeric-bypass path (`answer_from_hits()`) via Exa, same as
any other numeric query. Verified live: "what is the stock price of Tesla
and Amazon" → "Search results say $319.53." 72 passed locally, 84 on the
Pi.

### Added — Finnhub for stock/share price queries (Sprint 6)

`search_mcp.py::web_search()` now resolves stock/share-price-class queries
(`_STOCK_Q_RE`) via a real Finnhub quote (`_finnhub_stock_answer()`:
fuzzy company→ticker via Finnhub's own `/search`, then `/quote` for the
real price) instead of scraping a number out of search-snippet text.
Matched queries never fall back to Exa/Brave/Serper or the old regex
bypass on failure — they decline honestly instead, since that regex bypass
being unreliable is exactly why this tool exists. Other numeric-fact
classes (temperature, score, distance) are untouched — Finnhub only
covers finance, so `search_clean.py`'s existing bypass still handles those.
`answer_from_hits()` and its existing test are untouched — Finnhub is an
early exit before any search happens, so the two paths never overlap.

Found and fixed a real pre-existing bug along the way: `.env.example` and
the Pi's `.env` both had the key misspelled `FINHUB_API_KEY` (one N) —
would have silently never matched regardless of a real key being
configured. Fixed both (value-blind rename on the Pi).

4 new tests; 75 passed locally, 87 on the Pi. Verified live against 3 real
queries with correct symbol resolution and accurate price/change data.

### Fixed — soul.md, router coverage, summarizer language (found live, 2026-08-06)

Three real bugs from a live conversation transcript with all five tools
enabled (`config.oem_v002_test.yaml`):

- **`prompts/soul.md` described tools as LLM-invoked** ("You have these
  available tools: [...] use web_search when...", user-edited against the
  hailo-apps `agent_tools_example` reference). Wrong for this architecture
  — tool routing is 100% deterministic (`edge_harness/router.py`, before
  the LLM ever runs); the chat LLM has no function-calling wired up at all,
  so describing tools to it doesn't grant new capability, it just burns
  prompt budget and risks confusing output. Rewritten to state tools run
  automatically before the LLM sees the message, and it must never emit
  JSON/tool syntax.
- **`_WEB_RE` too narrow**: "current status of gas supply in Germany" fell
  through the router (no match) to the chat LLM, which correctly declined
  since it genuinely can't search — but the query should have routed to
  `web_search` in the first place. Added `status of` / `situation in|with|of`
  coverage. New test: `test_route_matches_status_and_situation_phrasings`.
- **Summarizer answered entirely in German** when evidence was German
  ("Die Hauptquellen für aktuelle Nachrichten..."). `SUMMARIZER_SYSTEM` had
  no explicit language instruction — added "always answer in English, even
  if evidence is in another language."

72 passed locally, 84 on the Pi. Verified the router fix live
(`route_and_execute("what is the current status of gas supply in Germany")`
→ `web_search`, previously `None`).

**Known issues, not fixed this pass** (same transcript, lower priority /
needs more diagnosis):
- Summarizer occasionally conflates distinct entities (stated both of
  Tamil Nadu's rival parties as "the current ruling party" before
  self-correcting on a later turn) — a synthesis-quality issue, would need
  a stronger "do not merge distinct entities" instruction and more testing
  to confirm it helps rather than just hides the symptom.
- Several turns produced no assistant response at all (not even a decline)
  — needs server-side logs to diagnose (likely an ASR/VAD empty-transcript
  case), not diagnosable from a client transcript alone.

### Fixed — `overlap_ms == 0` measurement bug (Sprint 1)

`pipeline.py::_stream_llm_to_tts()`'s `decode_end` timestamp was only ever
set inside the exception handler, never on the successful path — so
`overlap_ms` silently measured nothing on every normal turn. Fixed; also
hardened `first_complete_clause()` against false clause-boundary splits on
abbreviations ("Dr.", "PM.") and decimals ("3.14"). Full writeup:
`ROADMAP.md` §6d.

### Added — native C++ LLM backend, opt-in (Sprint 1b)

`csrc/hailo_llm_cpp.cpp`: a `pybind11` wrapper around `hailort::genai::LLM`
directly (bypassing `hailo_platform.genai`'s Python binding) with the GIL
explicitly released around the blocking decode loop. Fixes GIL contention
that was starving the TTS worker thread during decode — verified through
the real pipeline: `overlap_ms` 4274ms (was ~0). Opt-in via
`model.llm_backend: cpp` / `NOVA_HAILO_LLM_BACKEND=cpp`; default stays the
Python path. TTFT/decode throughput unchanged from the Python backend once
correctly joined to the shared VDevice group — the entire win is the
overlap fix, not a speed-up. Build: `scripts/build_hailo_llm_cpp.sh`
(Pi-only, needs HailoRT's CMake package + pybind11, not part of
`pip install -e .`). Tests: `tests/test_hailo_llm_cpp.py`. Full writeup:
`ROADMAP.md` §6d.

### Measured — FireRedVAD vs Silero vs WebRTC A/B (Sprint 2)

Desk-harness synthetic-tone run first (`scripts/run_model_matrix.py vad
--hardware`), then superseded by a **live recorded corpus**
(`scripts/record_vad_corpus.py` on the Pi, WM8960 @ 100% mic gain — 6 speech
prompts, 2 silence, 2 real background-noise takes; scored with
`scripts/run_model_matrix.py vad --hardware --live-corpus`) because the
synthetic-tone run scored all three candidates at 0% false-accept and
couldn't discriminate anything — sine-tone-vs-silence is trivial for any
VAD. Real audio does discriminate:

| Candidate | ok/10 | False-accept rate | Detection latency (mean / p50 / p95) |
| --- | --- | --- | --- |
| `vad_silero` | 10/10 | **0%** | 605 / 480 / 1305 ms |
| `vad_firered` | 9/10 | 20% | 425 / 465 / 645 ms |
| `vad_webrtc` | 6/10 | 40% | 230 / 150 / 630 ms |

WebRTC false-fired `speech_started` on **all 4** silence/noise rows — under
real room noise it isn't discriminating speech from ambient sound at all,
matching its published FLEURS-VAD-102 F1 of 52.3% vs Silero's 95.95%
(`ROADMAP.md` §6a). FireRedVAD is genuinely faster (~180ms lower mean
latency than Silero) but false-fired on 1 of 4 non-speech rows at its
current `threshold=0.5` — consistent with the existing caveat that it's a
generic, not target-speaker, VAD. Silero was clean on all 10 cases.

**Decision: keep Silero as default**, now confirmed on real recorded audio,
not just synthetic tones. 0% false-accept matters more for demo credibility
than ~150-200ms of extra detection latency, especially with real background
noise in the room. FireRedVAD stays a documented, tested A/B option
(`NOVA_HAILO_VAD=firered`); WebRTC is disqualified as a candidate for this
role. Raw results: `logs/model-matrix/sprint2-vad-live/`.

### Added — canonical action space, typed tool broker, capability profiles (Sprint 3)

Extracted `nova_hailo/edge_harness/` (`types.py`, `policy.py`, `router.py`,
`result_compressor.py`, `tool_broker.py`) from `oem_tools.py`'s
`OemToolGateway`, per the SCENIC canonical-action-space pattern in
`ROADMAP.md §2`: `route()` now returns a typed `RoutedIntent` (canonical
`Intent` + query + slots) instead of a bare string, `ToolBroker` is the sole
code path allowed to invoke `mcp_web_search`/`mcp_deep_research`/
`mcp_research_status`/the Workspace MCP tool classes (enforced by a new
grep-based test, not just a review claim), and `CapabilityProfile` replaces
the inline `enabled`/`write_enabled` set logic. `result_compressor.py` is
new (not moved): implements the "bounded typed block with an explicit
`instruction=` line" spec, truncating the body but never the instruction
line — staged for Sprint 5's Workspace tools return, not wired into the LLM
context path yet. `OemToolGateway` is now a thin facade preserving its exact
pre-Sprint-3 API; `pipeline.py` needed zero changes. 13 new tests
(`tests/test_edge_harness.py`); every pre-existing router/persona test
passes unchanged. 55 → 68 passed locally, 67 → 80 passed on the Pi.

### Measured — Brave vs Serper vs Tavily vs Exa websearch A/B (Sprint 4)

Live run on the Pi (`scripts/bench_websearch_providers.py`, real API keys, 4
queries × 6 provider/type combos). Serper live-timed-out on 1 of 4 queries
against our real `tools.timeout_sec: 1.5` production ceiling (Brave never
did); Exa `instant`/`fast` were both faster than Brave/Serper (mean
918/925ms vs 1207/1502ms) and returned richer per-result text, including a
literal price for the numeric query where Brave's snippet had none.
`exa.search(type="deep")` does not return a synthesized answer at any
`type` (needs `output_schema`) — wrong Exa endpoint for `deep_research`'s
role. **Correction, tested live against all 4 queries paired with Tavily:**
Exa's separate Agent product (`exa.agent.runs.create()`, async, billed) does
synthesize — mean 26.1s vs Tavily's 4.3s (~6.1x), flat $0.025/run,
consistently as-good-or-better quality (numeric: exact after-hours price +
citation vs Tavily's one sentence). Exa Agent's output is heavily
markdown-formatted, which is a liability for direct TTS (would read as
literal `##`/`**`/`[link]()` syntax) but a good fit for a saved document.
**Decision: `web_search` primary → Exa `fast`, fallback → Brave (Serper
demoted, timed out on 3 separate runs across different queries);
`deep_research`'s spoken summary stays on Tavily (prose already speakable,
~6x faster); Exa Agent becomes the concrete input for a new, separately
scoped Sprint 7 (Drive/Docs artifact storage) instead of a deep_research
replacement.** Full writeup:
`docs/benchmarks/nova-hailo-v0.0.2-sprint4-websearch-report.md`, raw data
`logs/bench-search/sprint4*.json`.

### Added — Exa-primary web_search, pulled forward from Sprint 5 (2026-08-06)

No reason to leave known-inferior Brave→Serper live in the shipped code
while other sprints ran once Sprint 4's evidence was solid. `search_mcp.py::
web_search()` now tries Exa (`type="fast"`) → Brave → Serper (Serper
demoted to last-resort, not removed), via a new `exa_search()` using raw
`httpx` (no `exa-py` dependency in the shipped pipeline, matching Brave/
Serper's existing style). Also fixed a real content bug this surfaced:
Exa's raw page-text extraction can include literal markdown (`# Weather
for...`) that would be spoken aloud as syntax — `_compact()` now strips
markdown links/headers/emphasis uniformly (no-op for Brave/Serper's already
-clean HTML snippets). `test_oem_web_search_fail_closed_without_keys`
updated to also clear `EXA_API_KEY` — the Pi's `.env` has a real one, so
without the delenv that test would silently make a live call there instead
of testing the fail-closed path. 80/80 on the Pi, verified live. New
`config.oem_v002_test.yaml` for testing `websearch_readonly`/`deep_research`
without touching `config.oem.yaml`'s demo default.

### Fixed — tool-result speech cut off mid-sentence (found live, 2026-08-06)

`validate_spoken_unit()`'s sentence-completeness check only asked "does
this end in `.?!`" — but "...on a deal. 2." and "...54°F (12." both do,
while being truncation artifacts (a cut-off list marker, a cut-off decimal)
not real sentences. Fixed by reusing `first_complete_clause()`'s existing
`_is_decimal_period`/`_is_abbreviation_period` logic inside
`validate_spoken_unit()` too, so trimming now finds the last *genuine*
sentence end. Root cause of the frequent mid-list cutoffs:
`summarizer_max_tokens: 48` was too small for a 2-3 item summary — raised
to 96 in `config.oem_v002_test.yaml`. Also added `model.llm_backend: cpp`
there so the test config now exercises the full stack together. 3 new
tests (`tests/test_oem_demo_offline.py`) reproducing both live examples
plus a control case. 71 passed locally, 83 on the Pi.

### Correction — Workspace tools were never actually blocked (2026-08-06)

Earlier same-day entries claimed Workspace tools were "genuinely blocked
on Google OAuth connect" -- checked the wrong path. The real token path
(`google_oauth.py::DEFAULT_TOKEN_PATH`, `runtime/google_oauth/tokens.json`)
has had a valid, authenticated token since 2026-07-30.
`check_calendar`/`check_email`/`list_drive_files` all tested live via
`route_and_execute()`, real API round-trips, `ok=True` on all three.
`config.oem_v002_test.yaml` now runs `profile: oem_readonly` with all five
tools enabled (`web_search`, `deep_research`, `check_calendar`,
`check_email`, `list_drive_files`).

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
