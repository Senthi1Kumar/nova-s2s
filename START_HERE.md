# Nova S2S — START HERE

Paste this file (or its key sections) as the opening context for any new
session in this repo. It distills what five sessions on the prior
`litert_lm_chat_app` taught us so this repo starts cold-but-informed.

## Mission

On-device (Jetson / Android head-unit) **in-vehicle voice assistant**:

- **Cascaded** pipeline — streaming ASR → Gemma-4 text LLM → streaming TTS
  (this is what buys <1-2s + barge-in; native-audio-in could not).
- Latency target: **TTFT/TTFB < 1–2s**, tracked as p50/75/90/95/99, per-turn
  and per-session, via a dedicated metrics endpoint + graph script.
- **12 simulated in-vehicle tools** (HVAC zones, sunroof/windows interlocks,
  reminders/calendar on host clock, media, fuel/range, etc.) + real MCP tools
  (calendar, spotify, email, WhatsApp, websearch, deep-research, OCR/img).
- **Modular agentic harness** (Lilian Weng harness patterns, *static* form
  first): tool router, ACE-style context playbook, sub-agents/backend jobs,
  file-system memory (EverOS), permission/confirmation gate.
- **Model**: base `gemma-4-E2B-it` (E4B/12B where hardware allows). Eval proved
  base > our v0.2 QLoRA fine-tune on every tool-call metric; the fine-tune only
  reintroduced instability. Do NOT reach for a fine-tune unless a new eval
  proves it closes the arg-slotting gap without regressing stability.
- **Engine**: LiteRT-LM built from source (edge-primary). HF Space (HF Pro GPU)
  is a demo mirror only.

## First move: MEASURE, don't optimize blind

Build **M1 (metrics + latency graph) before any optimization.** Add
`GET /api/metrics/percentiles` (per-turn ring buffer + per-session rollups for
ASR / TTFT / LLM tok-s / TTS / TTFB) and `scripts/latency_graph.py` (reads the
endpoint → matplotlib PNG tagged with model name + date + time). Then attack
whichever percentile is worst. Latency budget from prior live logs: **TTS
prompt time (0.4–1.8s) and the ~3127-token toolbox prefill dominate** — the
C-API binding overhead does NOT. Attack order: prefill-cache the static
system+tools block, per-turn tool routing (fewer tokens rendered), sentence-
chunked streaming TTS, speculative LLM prefill during ASR, smaller TTS.

## What WORKED (port / keep)

- **Lexical tool router** (`../litert_lm_chat_app/eval/tool_router.py`) —
  dependency-free, name×3/param×2/desc×1 token overlap, top-k. Zero latency,
  no model. Only add SmolLM2-135M / a router model IF eval shows recall
  failing. (Supra-Router-51M is edge-vs-cloud routing, NOT tool selection —
  wrong tool for us.)
- **SQLite CAN/OBD sim** (`../litert_lm_chat_app/app/vehicle_db.py`) — HVAC
  zone interlocks, sunroof/windows auto-close with HVAC, host-clock
  reminders/calendar. Engine-agnostic, solid — port verbatim.
- **Trained 12-tool schema** (`../litert_lm_chat_app/training/data_pipeline/
  intent_schema.py` → `build_toolbox`). Byte-for-byte match to training matters.
- **Eval harness** (`../litert_lm_chat_app/eval/`) — frozen seed split + OOD
  split + order-invariant scorer + gemma4_parse + `--toolbox native|full|routed`
  + percentile discipline. This is the anti-regression / anti-reward-hacking
  ruler. Port whole.
- **EverOS memory** (markdown-first, self-hostable, offline — right call over
  mem0/Postgres). Local shims: `llm_shim` (wraps engine as
  `/v1/chat/completions`) + `embed_shim` (sentence-transformers CPU as
  `/v1/embeddings`). Use **vector** search (hybrid needs an external rerank).
- **Runaway containment**: `cancel_process()` + a `max_output_tokens` cap +
  a char-length/marker/repetition watchdog.
- **UI language**: orb HUD + right-rail vehicle panel + left-rail metrics panel.
- **Sidecar auto-spawn** at startup: port-check + detached `subprocess.Popen`
  (`start_new_session=True`), binaries resolved from `sys.executable`'s dir
  (never trust `$PATH` — shells lie about venv activation).

## What FAILED — do NOT repeat

1. **Native-audio-in Gemma-4** can't prefill-while-speaking; encoder latency;
   never got under 2s reliably. → cascaded.
2. **Memory extraction contending with the single-tenant chat engine.** EverOS
   `/add` tripped its boundary detector mid-turn, ran the shim on the busy
   engine → extraction JSON leaked into the TTS stream + cross-thread
   "cannot release un-acquired lock". **RULE:** any memory/extraction LLM call
   runs ONLY when the chat engine is free (session close). `/add` is
   fire-and-forget, short timeout, NEVER on the turn path.
3. **Cactus engine**: CPU kernels are ARM-NEON-only (no x86 SIMD) — can't build
   on x86 dev box. LiteRT-LM builds on both; use it.
4. **litert_lm_api version drift**: per-call `max_output_tokens` is git-HEAD-
   only, not in published 0.13.x; `create_conversation` never sets the session
   cap. Build from source; feature-detect API surfaces.
5. **starlette/fastapi coupling**: EverOS needs new starlette; that breaks old
   fastapi's `TemplateResponse("name", {...})` → use `TemplateResponse(request,
   "name")`. Pin the trio together in one lockfile.
6. **Lazy model loading**: embed model loaded 4× in a race, blew timeouts
   mid-conversation. Pre-warm ALL models at startup, thread-safe singletons.
7. **Digit-unfaithful readback**: model says "8 percent" when the tool returned
   68%. Model behavior, not a bug. FIX: for numeric/factual results, speak the
   RESULT payload value, not the model's paraphrase (result-grounded confirms).
8. **Context overflow**: fixed ~3127-token toolbox floor starved a 4k window →
   persona collapse. → ACE itemized context playbook + prefill-cache static
   block + per-turn routing so only top-k tools render.
9. **Web search returns text only** (Brave: title/url/desc/age, no structured
   data) → model confabulates numbers (wrong stock prices). For numeric facts,
   use a real quote API or a "only state a number you can literally quote"
   guardrail.

## Harness design (Weng patterns, static form)

- Goal-loop: route → (plan) → act → observe → speak; proactive clarify.
- File system as memory: EverOS markdown + per-session workspace dir; sub-agent
  outputs are files/logs, not transient context.
- Sub-agents as tracked background jobs (run_id, status, SSE) — voice loop never
  blocks; proactive "the research is back" announce (IRIS/Vui pattern).
- Two-step confirmation gate for irreversible real-world tools (email send,
  calendar write), enforced in code — also the trust fix for phantom actions.
- Defer the self-improving meta-loop (Self-Harness / evolutionary search) to
  M6, gated behind a mature eval harness (else reward-hacking).

## Milestones

M0 repo + LiteRT-LM from source · **M1 metrics/latency (FIRST)** · M2 cascaded
core loop (TTFB p50 <2s) · M3 harness layer · M4 real MCP tools + sub-agents ·
M5 HF Space + live cam · M6 (later) self-improving harness.

## Standing constraints (persistent, all sessions)

- Never run `git add`/`git commit` — suggest files + message only.
- The user runs all pipeline/training/eval/app commands and pastes output.
- Don't debug with many small bash commands — write correct scripts upfront.
- Reference the prior repo at `../litert_lm_chat_app/` for proven code to port.
