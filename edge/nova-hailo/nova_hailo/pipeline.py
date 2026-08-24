"""Cascaded Nova turn on Hailo-10H with streaming LLM→TTS overlap."""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from hailo_platform import VDevice

try:
    from hailo_apps.python.core.common.defines import SHARED_VDEVICE_GROUP_ID
    from hailo_apps.python.core.common.hailo_logger import get_logger
    from hailo_apps.python.gen_ai_apps.gen_ai_utils.llm_utils import streaming as streaming_utils
except ImportError:
    SHARED_VDEVICE_GROUP_ID = "SHARED"
    import logging

    get_logger = lambda name: logging.getLogger(name)  # noqa: E731
    streaming_utils = None

from nova_hailo.auth import AuthDecision, DriveAuthPrecheck, looks_like_payment_tool
from nova_hailo.backends.llm import HailoLLM, HailoLLMCpp
from nova_hailo.backends.stt import WhisperSTT
from nova_hailo.backends.tts import clean_text_for_tts, create_tts
from nova_hailo.config import AppConfig, resolve_audio_device_ids, resolve_llm_hef
from nova_hailo.context_history import ConversationHistory
from nova_hailo.edge_harness.controller import (
    gate_after_router,
    is_thin_utterance,
)
from nova_hailo.edge_harness.interrupt import GenerationGuard
from nova_hailo.edge_harness.memory_store import MemoryStore
from nova_hailo.edge_harness.router import CLARIFY_SPEAK
from nova_hailo.edge_harness.speak_sanitize import sanitize_for_tts
from nova_hailo.edge_harness.task_state import TaskState, record_clarify
from nova_hailo.metrics import SessionMetrics, Timer, TurnMetrics
from nova_hailo.response_contract import first_complete_clause, validate_spoken_unit
from nova_hailo.web.fail_closed import FAIL_CLOSED_SPEAK
from nova_hailo.stream_text import SentenceBuffer
from nova_hailo.tools.oem_tools import OemToolGateway
from nova_hailo.tools.registry import (
    build_default_tools,
    execute_tool,
    tools_prompt_block,
)
from nova_hailo.tools.research_jobs import (
    POLL_INTERVAL_SEC,
    STATUS_DONE,
    STATUS_FAILED,
)
from nova_hailo.tools.search_summarizer import (
    DEFAULT_SUMMARIZER_MAX_TOKENS,
    summarize_evidence,
)

logger = get_logger(__name__)


def _clean_speak_text(text: str) -> str:
    if not text:
        return ""
    if streaming_utils is not None:
        try:
            text = streaming_utils.clean_response(text)
        except Exception:
            pass
    return clean_text_for_tts(text)


@dataclass
class TurnResult:
    user_text: str
    assistant_text: str
    metrics: TurnMetrics
    auth_decision: str
    tool_name: str | None = None
    research_job_id: str | None = None


class NovaPipeline:
    """
    Cascaded turn with streaming TTS overlap (hailo-apps voice_assistant pattern):

      audio → Whisper → DriveAuth → LLM token stream → Piper clauses (CPU∥NPU)
    """

    def __init__(
        self,
        cfg: AppConfig,
        llm_hef: str | None = None,
        whisper_hef: str | None = None,
        no_tts: bool = False,
        text_only: bool = False,
        audio_in: str | None = None,
        audio_out: str | None = None,
    ):
        self.cfg = cfg
        self.abort_event = threading.Event()
        self.text_only = text_only
        self.session = SessionMetrics()
        # Optional streaming hooks (web realtime)
        self.on_token: Callable[[str], None] | None = None
        self.on_audio_pcm: Callable[[np.ndarray, int], None] | None = None
        self.on_validated_clause: Callable[[str], None] | None = None
        self.on_research_status: Callable[[dict], None] | None = None
        # Lightweight UI toast: {name, status, detail?} for tool/MCP calls.
        self.on_tool_status: Callable[[dict], None] | None = None
        self._barge_in_flag = False
        self._genai_lock = threading.Lock()
        self.active_generation_id = 0
        self.gen_guard = GenerationGuard()
        mem_path = cfg.get("pipeline", "memory_path", default=None)
        self.memory = MemoryStore(mem_path)

        self.audio_in_id, self.audio_out_id = resolve_audio_device_ids(
            audio_in or cfg.get("voice", "input_device"),
            audio_out or cfg.get("voice", "output_device"),
        )

        params = VDevice.create_params()
        params.group_id = SHARED_VDEVICE_GROUP_ID
        self.vdevice = VDevice(params)

        self.stt: WhisperSTT | None = None
        self._whisper_hef = whisper_hef or cfg.get("model", "whisper_hef")
        # OEM default: resident STT (sequential_stt=false). Cold Whisper ~2.6s breaks TTFA.
        self.sequential_stt = bool(cfg.get("pipeline", "sequential_stt", default=False))
        self.stream_tts = bool(cfg.get("pipeline", "stream_tts", default=True))
        self.voice_one_sentence = bool(cfg.get("pipeline", "voice_one_sentence", default=True))
        self.first_chunk_min_chars = int(cfg.get("pipeline", "first_chunk_min_chars", default=24))
        self.wait_tts_drain = bool(cfg.get("voice", "wait_tts_drain", default=True))
        self.research_poll_timeout_sec = float(
            cfg.get("tools", "research_poll_timeout_sec", default=45) or 45
        )
        self.summarize_search = bool(cfg.get("tools", "summarize_search", default=False))
        self.summarizer_max_tokens = int(
            cfg.get("tools", "summarizer_max_tokens", default=DEFAULT_SUMMARIZER_MAX_TOKENS)
            or DEFAULT_SUMMARIZER_MAX_TOKENS
        )
        # validate_spoken_unit()'s own default (220 chars) is sized for casual
        # chat's "one short spoken sentence" pacing. Tool/deep_research speak
        # is a different context -- the user explicitly asked for depth, so it
        # gets its own, larger budget instead of being silently cut back down
        # after summarizer_max_tokens already let it generate longer.
        self.tool_speak_max_chars = int(cfg.get("tools", "max_speak_chars", default=700) or 700)
        self.validate_before_speak = bool(
            cfg.get("pipeline", "validate_before_speak", default=True)
        )
        self.genai_serial = bool(cfg.get("pipeline", "genai_serial", default=True))
        self.max_history_turns = int(cfg.get("pipeline", "max_history_turns", default=2))
        self.history = ConversationHistory(max_turns=self.max_history_turns)

        self.stt_engine = str(cfg.get("model", "stt_engine", default="whisper_hef") or "whisper_hef")

        if not text_only and not self.sequential_stt:
            self.stt = self._make_stt()
            print(f"STT resident [{self.stt_engine}]: {self.stt.hef_path}")
        elif not text_only:
            print("STT mode: on-demand (load→transcribe→release each turn)")

        llm_path = resolve_llm_hef(llm_hef or cfg.get("model", "llm_hef"))
        self.no_think = bool(cfg.get("llm", "no_think", default=False))
        if cfg.get("model", "no_think") is not None:
            nt = cfg.get("model", "no_think")
            if nt is not None:
                self.no_think = bool(nt)
        # Opt-in native LLM backend (ROADMAP.md #6d): explicit-GIL-
        # release C++ wrapper around hailort::genai::LLM directly, fixing GIL
        # contention that starved the TTS worker thread during decode. Not the
        # default -- only via model.llm_backend: cpp or NOVA_HAILO_LLM_BACKEND.
        llm_backend = (
            os.environ.get("NOVA_HAILO_LLM_BACKEND")
            or cfg.get("model", "llm_backend", default="python")
            or "python"
        ).strip().lower()
        LLMCls = HailoLLMCpp if llm_backend == "cpp" else HailoLLM
        self.llm = LLMCls(
            self.vdevice,
            llm_path,
            temperature=float(cfg.get("model", "temperature", default=0.15)),
            seed=int(cfg.get("model", "seed", default=42)),
            max_tokens=int(cfg.get("model", "max_tokens", default=24)),
            no_think=self.no_think,
        )
        # Optional dedicated summarizer HEF (true 2nd agent). Same VDevice + GenAI
        # lock; if unset or same path as chat, reuse self.llm. Skipped entirely
        # when summarize_search is off (search speak = tool/cleaner fallback).
        self.summarizer_llm: HailoLLM | None = None
        self._summarizer_owns_llm = False
        summarizer_hef = str(cfg.get("tools", "summarizer_hef", default="") or "").strip()
        if self.summarize_search and summarizer_hef:
            try:
                sum_path = resolve_llm_hef(summarizer_hef)
                if sum_path != llm_path:
                    self.summarizer_llm = HailoLLM(
                        self.vdevice,
                        sum_path,
                        temperature=0.1,
                        seed=int(cfg.get("model", "seed", default=42)),
                        max_tokens=int(
                            cfg.get(
                                "tools",
                                "summarizer_max_tokens",
                                default=DEFAULT_SUMMARIZER_MAX_TOKENS,
                            )
                            or DEFAULT_SUMMARIZER_MAX_TOKENS
                        ),
                        no_think=True,
                    )
                    self._summarizer_owns_llm = True
                    print(f"Summarizer LLM (2nd agent): {sum_path}")
                else:
                    print("Summarizer LLM: reusing chat HEF (summarizer_hef == llm_hef)")
            except Exception as exc:  # noqa: BLE001
                print(f"[tools] summarizer_hef={summarizer_hef!r} failed ({exc}); using chat LLM")
        self.tools_enabled = bool(cfg.get("tools", "enable_in_prompt", default=False))

        tts_off = no_tts or bool(cfg.get("voice", "no_tts", default=False))
        piper = cfg.get("model", "piper_onnx") or None
        if piper and not Path(piper).is_absolute():
            from nova_hailo.config import ROOT

            piper = str(ROOT / piper)
        wait_tts = bool(cfg.get("voice", "wait_tts", default=False))
        playback = str(cfg.get("voice", "playback", default="wm8960") or "wm8960").lower()
        local_play = playback in {"wm8960", "both", "local"}
        tts_engine = str(cfg.get("model", "tts_engine", default="kokoro") or "kokoro")
        kokoro_voice = str(cfg.get("model", "kokoro_voice", default="af_sarah") or "af_sarah")
        self.tts = create_tts(
            tts_engine,
            enabled=not tts_off,
            piper_path=piper,
            kokoro_voice=kokoro_voice,
            inflect_model_dir=cfg.get("model", "inflect_model_dir", default=None),
            inflect_speed=float(cfg.get("model", "inflect_speed", default=1.0) or 1.0),
            inflect_variation=float(
                cfg.get("model", "inflect_variation", default=0.667) or 0.667
            ),
            inflect_seed=int(cfg.get("model", "inflect_seed", default=7) or 7),
            device_id=self.audio_out_id,
            wait_for_play=wait_tts,
            local_play=local_play,
            pcm_callback=self._pcm_sink,
        )

        enabled = cfg.get("tools", "enabled", default=None)
        self.tools = build_default_tools(enabled if self.tools_enabled else [])
        tool_profile = str(cfg.get("tools", "profile", default="off") or "off")
        self.oem_gateway: OemToolGateway | None = None
        # "conversation" keeps identity / smalltalk / honest declines with no tools.
        # "websearch_readonly" enables Brave→Serper + async Tavily research (v0.0.1).
        # "oem_readonly" additionally enables Workspace tools.
        if tool_profile in {"oem_readonly", "conversation", "websearch_readonly"}:
            self.oem_gateway = OemToolGateway(
                enabled=list(enabled or []),
                timeout_sec=float(cfg.get("tools", "timeout_sec", default=1.5)),
                write_enabled=bool(cfg.get("tools", "write_enabled", default=False)),
                serper_fallback=bool(cfg.get("tools", "serper_fallback", default=False)),
            )
            self.system_prompt = cfg.soul_prompt + (
                "\n\nSpeak naturally in one or two short sentences. "
                "Answer the user directly. Do not output JSON or tool calls. "
                "When a tool result is provided in the user message, speak only from that result."
            )
        elif self.tools_enabled:
            self.system_prompt = cfg.soul_prompt + "\n\n" + tools_prompt_block(self.tools)
        else:
            self.system_prompt = cfg.soul_prompt + (
                "\n\nDo not call tools. Do not output JSON. "
                "Speak naturally in at most two short sentences."
            )
        if self.no_think:
            self.system_prompt = (
                " /no_think\n"
                + self.system_prompt
                + "\nNever write <think> tags or step-by-step reasoning. Final answer only."
            )

        self.auth = DriveAuthPrecheck(
            enabled=bool(cfg.get("auth", "enabled", default=False)),
            payment_keywords=cfg.get("auth", "payment_keywords", default=[]),
            confirm_keywords=cfg.get("auth", "confirm_keywords", default=[]),
            deny_keywords=cfg.get("auth", "deny_keywords", default=[]),
        )
        self.multi_tool_rounds = int(cfg.get("pipeline", "multi_tool_rounds", default=1))
        self.clear_each = bool(cfg.get("pipeline", "clear_context_each_turn", default=False))
        # Native device-side context reuse (see _messages / _maybe_compact_context).
        self.native_context = bool(cfg.get("pipeline", "native_context", default=False))
        self.ctx_compact_ratio = float(
            cfg.get("pipeline", "ctx_compact_ratio", default=0.7) or 0.7
        )
        self._native_seeded = False
        # Probe context capacity once (HailoRT 5.1.1 GenAI)
        self.context_capacity = None
        try:
            self.context_capacity = int(self.llm.llm.max_context_capacity())
            print(f"LLM max_context_capacity={self.context_capacity}")
        except Exception:
            pass
        print(
            f"Pipeline: stream_tts={self.stream_tts} voice_one_sentence={self.voice_one_sentence} "
            f"max_tokens={self.llm.max_tokens} history={self.max_history_turns} "
            f"oem_tools={tool_profile} sequential_stt={self.sequential_stt}"
        )

    def poll_research_job(self, job_id: str) -> dict:
        """Poll a deep_research job started by the OEM gateway / FastMCP broker."""
        if self.oem_gateway is None:
            return {
                "ok": False,
                "name": "research_status",
                "status": "failed",
                "reason": "no_gateway",
                "speak": "I can't reach that service right now.",
                "result": None,
            }
        return self.oem_gateway.poll_research(job_id)

    def _emit_tool_status(self, name: str, status: str, *, detail: str = "") -> None:
        """Notify UI of a tool/MCP call (best-effort; never raises into the turn)."""
        cb = self.on_tool_status
        if cb is None:
            return
        try:
            cb({"name": name, "status": status, "detail": detail or None})
        except Exception:
            pass

    def _maybe_summarize_search(
        self,
        *,
        tool_name: str | None,
        tool_speak: str,
        tool_payload: dict | None,
        user_text: str,
        metrics: TurnMetrics,
    ) -> str:
        """Grounded summarizer on cleaned evidence; numeric bypass; fallback on fail."""
        if not self.summarize_search or tool_name not in {"web_search", "deep_research"}:
            return tool_speak
        payload = tool_payload if isinstance(tool_payload, dict) else {}
        if payload.get("numeric"):
            metrics.notes.append("search_summarizer:bypass_numeric")
            return tool_speak
        evidence = str(payload.get("evidence") or "").strip()
        needs = payload.get("needs_summary")
        # needs_summary False → skip; True/None with evidence → summarize
        if needs is False or not evidence:
            if not evidence:
                metrics.notes.append("search_summarizer:skip_no_evidence")
            return tool_speak
        sum_llm = self.summarizer_llm if self.summarizer_llm is not None else self.llm
        agent_tag = "hef2" if self._summarizer_owns_llm else "chat_hef"
        spoken = summarize_evidence(
            sum_llm,
            user_text,
            evidence,
            max_tokens=self.summarizer_max_tokens,
            with_genai=self._with_genai,
        )
        # Always clear chat KV after a summarizer turn (shared lock / shared HEF).
        self._native_seeded = False
        try:
            self.llm.clear()
        except Exception:
            pass
        if self._summarizer_owns_llm and self.summarizer_llm is not None:
            try:
                self.summarizer_llm.clear()
            except Exception:
                pass
        if spoken:
            metrics.notes.append(f"search_summarizer:ok:{agent_tag}")
            return spoken
        metrics.notes.append(f"search_summarizer:fail:{agent_tag}")
        return tool_speak

    def _await_research_job(self, job_id: str) -> dict:
        deadline = time.monotonic() + float(self.research_poll_timeout_sec)
        last_status: str | None = None
        out: dict = {}
        while time.monotonic() < deadline:
            if self.abort_event.is_set():
                return {
                    "ok": False,
                    "status": STATUS_FAILED,
                    "speak": "Okay, stopping.",
                    "reason": "aborted",
                    "result": {"job_id": job_id},
                }
            out = self.poll_research_job(job_id)
            status = str(out.get("status") or "")
            if status != last_status:
                last_status = status
                if self.on_research_status is not None:
                    try:
                        self.on_research_status(
                            {
                                "job_id": job_id,
                                "status": status,
                                "speak": out.get("speak"),
                            }
                        )
                    except Exception:
                        pass
            if status in {STATUS_DONE, STATUS_FAILED}:
                return out
            time.sleep(POLL_INTERVAL_SEC)
        return {
            "ok": False,
            "status": STATUS_FAILED,
            "speak": "That research is taking too long.",
            "reason": "timeout",
            "result": {"job_id": job_id},
        }

    def _speak_tool_line(
        self,
        speak: str,
        *,
        wall0: float,
        metrics: TurnMetrics,
        user_text: str,
        auth_decision: str,
        tool_name: str | None,
        record_session: bool,
        research_job_id: str | None = None,
        add_history: bool = True,
    ) -> TurnResult:
        speak = sanitize_for_tts(speak, fallback="I'm here.")
        contract = validate_spoken_unit(speak, max_chars=self.tool_speak_max_chars)
        speak = sanitize_for_tts(str(contract["speak"] or ""), fallback="I'm here.")
        if not self.gen_guard.should_enqueue_pcm(self.gen_guard.generation_id):
            metrics.notes.append("pcm_invalidated")
            metrics.total_latency_ms = (time.perf_counter() - wall0) * 1000
            if record_session:
                self.session.add(metrics)
            return TurnResult(
                user_text,
                speak,
                metrics,
                auth_decision,
                tool_name,
                research_job_id=research_job_id,
            )
        if self.on_validated_clause is not None:
            try:
                self.on_validated_clause(speak)
            except Exception:
                pass
        gid = self.tts.begin_turn()
        self.tts.enqueue(speak, gid)
        if self.wait_tts_drain:
            self.tts.wait_drain(timeout=60)
        snap = self.tts.timing_snapshot()
        if snap.get("ttfa_wall") is not None and metrics.ttfa_ms is None:
            metrics.ttfa_ms = (snap["ttfa_wall"] - wall0) * 1000
        if metrics.ttfb_ms is None:
            metrics.ttfb_ms = metrics.ttfa_ms
        metrics.tts_ms = snap.get("tts_ms")
        metrics.tts_synth_ms = snap.get("tts_synth_ms")
        metrics.tts_play_ms = snap.get("tts_play_ms")
        metrics.total_latency_ms = (time.perf_counter() - wall0) * 1000
        if add_history:
            self.history.add(user_text, speak, tool_summary=speak[:160])
        if record_session:
            self.session.add(metrics)
        return TurnResult(
            user_text,
            speak,
            metrics,
            auth_decision,
            tool_name,
            research_job_id=research_job_id,
        )

    def close(self):
        self.abort_event.set()
        self.tts.stop()
        self._release_stt()
        if self._summarizer_owns_llm and self.summarizer_llm is not None:
            try:
                self.summarizer_llm.release()
            except Exception:
                pass
            self.summarizer_llm = None
        if self.llm:
            self.llm.release()
        try:
            self.vdevice.release()
        except Exception:
            pass
        import gc

        gc.collect()

    def _release_stt(self):
        if self.stt is not None:
            try:
                self.stt.release()
            except Exception:
                pass
            self.stt = None
            import gc

            gc.collect()

    def _make_stt(self):
        """Build the configured STT engine, falling back to Whisper on the NPU.

        parakeet runs on CPU, which also frees the NPU for the LLM. If its model
        or shared library is missing we fall back rather than fail the demo.
        """
        if self.stt_engine == "parakeet":
            from nova_hailo.backends.parakeet_stt import ParakeetSTT, resolve_parakeet_paths

            model, lib = resolve_parakeet_paths(
                self.cfg.get("model", "parakeet_model"),
                self.cfg.get("model", "parakeet_lib"),
            )
            if model and lib:
                try:
                    return ParakeetSTT(
                        model,
                        lib,
                        decoder=str(self.cfg.get("model", "parakeet_decoder", default="tdt")),
                        language=self.cfg.get("model", "parakeet_language"),
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[stt] parakeet init failed ({exc}); falling back to Whisper HEF")
            else:
                print(f"[stt] parakeet artifacts missing (model={model} lib={lib}); using Whisper HEF")
            self.stt_engine = "whisper_hef"
        elif self.stt_engine == "nemo_speech":
            from nova_hailo.backends.nemo_speech_stt import (
                NemoSpeechSTT,
                resolve_nemo_speech_paths,
                resolve_nemo_speech_vad_path,
            )

            model, lib = resolve_nemo_speech_paths(
                self.cfg.get("model", "nemo_speech_model"),
                self.cfg.get("model", "nemo_speech_lib"),
            )
            if model and lib:
                try:
                    return NemoSpeechSTT(
                        model,
                        lib,
                        language=self.cfg.get("model", "nemo_speech_language"),
                        vad_model=resolve_nemo_speech_vad_path(
                            self.cfg.get("model", "nemo_speech_vad_model")
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[stt] nemo_speech init failed ({exc}); falling back to Whisper HEF")
            else:
                print(f"[stt] nemo_speech artifacts missing (model={model} lib={lib}); using Whisper HEF")
            self.stt_engine = "whisper_hef"
        return WhisperSTT(self.vdevice, hef_path=self._whisper_hef)

    def _ensure_stt(self):
        if self.stt is None:
            self.stt = self._make_stt()
        return self.stt

    def _pcm_sink(self, audio: np.ndarray, sample_rate: int):
        cb = self.on_audio_pcm
        if cb is not None:
            try:
                cb(audio, sample_rate)
            except Exception:
                pass

    def on_abort(self):
        self._barge_in_flag = True
        self.active_generation_id += 1  # invalidate in-flight emitters
        self.gen_guard.invalidate("barge_in")
        self.abort_event.set()
        self.tts.interrupt()

    def clear_context(self):
        self.llm.clear()
        self.history.clear()
        self.auth.reset_session()
        print("Context cleared.")

    def startup_greet(self) -> str:
        """Compact last-session greet. Never dumps a full transcript."""
        return self.memory.startup_greet()

    def begin_turn(self):
        """Clear abort/barge-in state before a new user turn."""
        self._barge_in_flag = False
        self.abort_event.clear()
        self.tts.clear_interruption()
        self.gen_guard.begin()

    def _messages(self, user_text: str, *, tool_result_speak: str | None = None) -> list:
        # Native mode: HailoRT GenAI keeps conversation context on-device between
        # generate() calls (5.1.1 API ref: "context is automatically maintained"),
        # so after seeding we send only the new user turn. Prefill drops from
        # system+history (~150-250 tok) to ~10-20 tok, and the model sees the real
        # conversation instead of a reconstruction.
        if self.native_context and self._native_seeded:
            msgs: list = []
            if tool_result_speak:
                msgs.append({"role": "system", "content": f"Tool result: {tool_result_speak}"})
            msgs.append({"role": "user", "content": user_text})
            return msgs
        # Seeding turn (first, or first after a compaction) carries the system prompt.
        self._native_seeded = bool(self.native_context)
        return self.history.build_messages(
            self.system_prompt,
            user_text,
            tool_result_speak=tool_result_speak,
            no_think=self.no_think,
        )

    def _context_stats(self) -> tuple[int | None, int | None]:
        """(used, capacity) device-side context tokens, or (None, None)."""
        raw = getattr(self.llm, "llm", None)
        if raw is None:
            return None, None
        try:
            return int(raw.get_context_usage_size()), int(raw.max_context_capacity())
        except Exception:
            return None, None

    def _maybe_compact_context(self, metrics=None) -> None:
        """Clear device context before it overflows the compiled 2048 ceiling.

        Capacity is fixed at HEF compile time and cannot be raised, so a long
        session must reset and re-seed rather than grow.
        """
        used, cap = self._context_stats()
        if not used or not cap:
            return
        if used >= cap * self.ctx_compact_ratio:
            self.llm.clear()
            self.history.clear()
            self._native_seeded = False
            if metrics is not None:
                metrics.notes.append(f"ctx_compact:{used}/{cap}")
            print(f"[ctx] compacted at {used}/{cap} tokens — re-seeding system prompt")

    def _with_genai(self, fn):
        if self.genai_serial:
            with self._genai_lock:
                return fn()
        return fn()


    def run_audio_turn(self, audio) -> TurnResult:
        if self.text_only:
            raise RuntimeError("STT not available in text_only mode")
        self.begin_turn()
        wall0 = time.perf_counter()
        load_ms = 0.0
        infer_ms = 0.0

        t_load = Timer()
        stt = self._ensure_stt()
        load_ms = t_load.ms()
        try:
            t_inf = Timer()

            def _stt():
                return stt.transcribe(audio)

            transcript = self._with_genai(_stt)
            infer_ms = t_inf.ms()
        finally:
            if self.sequential_stt:
                print("[mem] releasing Whisper STT before LLM…")
                self._release_stt()

        stt_ms = load_ms + infer_ms
        print(f"You: {transcript}")
        if not transcript:
            m = TurnMetrics(
                stt_ms=stt_ms,
                stt_load_ms=load_ms,
                stt_infer_ms=infer_ms,
                total_latency_ms=(time.perf_counter() - wall0) * 1000,
            )
            m.notes.append("empty transcript")
            self.session.add(m)
            return TurnResult("", "", m, AuthDecision.BYPASS.value)

        result = self.run_text_turn(
            transcript,
            stt_ms=stt_ms,
            wall0=wall0,
            record_session=True,
            begin=False,
        )
        result.metrics.stt_load_ms = load_ms
        result.metrics.stt_infer_ms = infer_ms
        return result

    def run_text_turn(
        self,
        user_text: str,
        stt_ms: float | None = None,
        wall0: float | None = None,
        record_session: bool = True,
        begin: bool = True,
    ) -> TurnResult:
        if begin:
            self.begin_turn()
        wall0 = wall0 if wall0 is not None else time.perf_counter()
        metrics = TurnMetrics()
        metrics.stt_ms = stt_ms
        if self.stream_tts:
            metrics.notes.append("stream_tts=on")
        if self.no_think:
            metrics.notes.append("no_think=on")
        if not self.sequential_stt:
            metrics.notes.append("resident_stt")
        user_text = (user_text or "").strip()
        if not user_text or is_thin_utterance(user_text):
            metrics.notes.append("fail_closed_thin" if user_text else "empty transcript")
            return self._speak_tool_line(
                FAIL_CLOSED_SPEAK,
                wall0=wall0,
                metrics=metrics,
                user_text=user_text,
                auth_decision=AuthDecision.BYPASS.value,
                tool_name=None,
                record_session=record_session,
                add_history=False,
            )

        if self.clear_each:
            self.llm.clear()
            self.history.clear()
            self._native_seeded = False
        elif self.native_context:
            # Keep device KV across turns; only reset when nearing the 2048 ceiling.
            self._maybe_compact_context(metrics)
        else:
            # Reconstruct bounded prompt each turn; clear device context before generate
            self.llm.clear()

        auth_t = Timer()
        auth = self.auth.precheck(user_text)
        metrics.auth_ms = auth_t.ms()
        metrics.auth_decision = auth.decision.value
        print(f"[auth] {auth.decision.value}")

        if auth.decision in (AuthDecision.STEP_UP, AuthDecision.DENIED):
            speak = auth.speak or ""
            metrics.ttfb_ms = (time.perf_counter() - wall0) * 1000
            print(f"Assistant: {speak}")
            gid = self.tts.begin_turn()
            self.tts.enqueue(speak, gid)
            if self.wait_tts_drain:
                self.tts.wait_drain(timeout=60)
            snap = self.tts.timing_snapshot()
            metrics.tts_ms = snap.get("tts_ms")
            metrics.tts_synth_ms = snap.get("tts_synth_ms")
            metrics.tts_play_ms = snap.get("tts_play_ms")
            if snap.get("ttfa_wall") is not None:
                metrics.ttfa_ms = (snap["ttfa_wall"] - wall0) * 1000
            metrics.total_latency_ms = (time.perf_counter() - wall0) * 1000
            if record_session:
                self.session.add(metrics)
            return TurnResult(user_text, speak, metrics, auth.decision.value)

        # OEM allowlist tools before LLM (deterministic, fail-closed)
        tool_name = None
        tool_speak = None
        if self.oem_gateway is not None:
            tool_t = Timer()
            tool_out = self.oem_gateway.route_and_execute(user_text)
            metrics.tool_ms = tool_t.ms()
            if getattr(self.oem_gateway, "last_clear_llm", False) and not self.clear_each:
                try:
                    self.llm.clear()
                    self.history.clear()
                    self._native_seeded = False
                    metrics.notes.append("task_boundary_clear")
                except Exception:
                    pass
            if tool_out is not None:
                tool_name = tool_out.get("name")
                tool_speak = str(tool_out.get("speak") or "")
                metrics.tool_name = tool_name
                metrics.notes.append(f"oem_tool:{tool_name}:{tool_out.get('status')}")
                self._emit_tool_status(
                    str(tool_name or "tool"),
                    "running",
                    detail=str(tool_out.get("status") or ""),
                )
                # Fast path: tool speak (optional grounded summarizer) then TTS
                if tool_out.get("ok") and tool_speak:
                    if tool_name in {
                        "web_search",
                        "deep_research",
                        "check_calendar",
                        "check_email",
                        "list_drive_files",
                    }:
                        try:
                            self.memory.add(f"{tool_name}: {user_text[:80]}")
                        except Exception:
                            pass
                    job_id = None
                    if tool_name == "deep_research":
                        job_id = (tool_out.get("result") or {}).get("job_id")
                    # Summarize sync search before speaking; skip filler for research.
                    if not job_id:
                        tool_speak = self._maybe_summarize_search(
                            tool_name=tool_name,
                            tool_speak=tool_speak,
                            tool_payload=tool_out.get("result"),
                            user_text=user_text,
                            metrics=metrics,
                        )
                    result = self._speak_tool_line(
                        tool_speak,
                        wall0=wall0,
                        metrics=metrics,
                        user_text=user_text,
                        auth_decision=auth.decision.value,
                        tool_name=tool_name,
                        record_session=False,
                        research_job_id=job_id,
                        add_history=job_id is None,
                    )
                    if not job_id:
                        self._emit_tool_status(str(tool_name or "tool"), "done")
                        if record_session:
                            self.session.add(metrics)
                        return result
                    # Async research: poll FSM, summarize evidence, then speak.
                    final = self._await_research_job(str(job_id))
                    final_speak = str(final.get("speak") or "I couldn't finish that research.")
                    final_payload = final.get("result") if isinstance(final.get("result"), dict) else {}
                    if final.get("evidence") and not final_payload.get("evidence"):
                        final_payload = {
                            **final_payload,
                            "evidence": final.get("evidence"),
                            "needs_summary": True,
                            "numeric": False,
                        }
                    final_speak = self._maybe_summarize_search(
                        tool_name=tool_name,
                        tool_speak=final_speak,
                        tool_payload=final_payload,
                        user_text=user_text,
                        metrics=metrics,
                    )
                    metrics.notes.append(
                        f"research_job:{job_id}:{final.get('status')}"
                    )
                    term = "done" if final.get("ok") else "failed"
                    self._emit_tool_status(
                        str(tool_name or "deep_research"),
                        term,
                        detail=str(final.get("status") or term),
                    )
                    return self._speak_tool_line(
                        final_speak,
                        wall0=wall0,
                        metrics=metrics,
                        user_text=user_text,
                        auth_decision=auth.decision.value,
                        tool_name=tool_name,
                        record_session=record_session,
                        research_job_id=str(job_id),
                        add_history=True,
                    )
                # unavailable — still speak the honest unavailable line
                if not tool_out.get("ok") and tool_speak:
                    self._emit_tool_status(
                        str(tool_name or "tool"),
                        "failed",
                        detail=str(tool_out.get("status") or "unavailable"),
                    )
                    return self._speak_tool_line(
                        tool_speak,
                        wall0=wall0,
                        metrics=metrics,
                        user_text=user_text,
                        auth_decision=auth.decision.value,
                        tool_name=tool_name,
                        record_session=record_session,
                        add_history=False,
                    )

            # Unmatched / empty / thin: never unconstrained 1.5B free chat.
            state = getattr(self.oem_gateway, "task_state", None) or TaskState()
            decision = gate_after_router(user_text, state, None)
            if decision is not None and decision.invoke_llm:
                metrics.notes.append(f"host_gate:chat_llm")
                # Fall through to _agent_loop (bounded speak, not tool ReAct).
            elif decision is not None and decision.path == "force_search":
                try:
                    forced = self.oem_gateway.execute("web_search", query=user_text)
                except Exception:
                    forced = None
                if forced is not None:
                    tool_out = forced
                    tool_name = forced.get("name") or "web_search"
                    tool_speak = str(forced.get("speak") or "")
                    metrics.tool_name = tool_name
                    metrics.notes.append("host_gate:force_search")
                    if tool_speak:
                        if forced.get("ok"):
                            try:
                                self.memory.add(f"web_search: {user_text[:80]}")
                            except Exception:
                                pass
                        return self._speak_tool_line(
                            tool_speak,
                            wall0=wall0,
                            metrics=metrics,
                            user_text=user_text,
                            auth_decision=auth.decision.value,
                            tool_name=tool_name,
                            record_session=record_session,
                            add_history=bool(forced.get("ok")),
                        )
            elif decision is not None:
                if decision.path == "clarify":
                    try:
                        self.oem_gateway.task_state = record_clarify(state)
                    except Exception:
                        pass
                speak = decision.speak or CLARIFY_SPEAK
                metrics.notes.append(f"host_gate:{decision.path}")
                return self._speak_tool_line(
                    speak,
                    wall0=wall0,
                    metrics=metrics,
                    user_text=user_text,
                    auth_decision=auth.decision.value,
                    tool_name=None,
                    record_session=record_session,
                    add_history=False,
                )

        speak_text, tool_name2, llm_bundle = self._agent_loop(
            user_text,
            auth_accepted=(auth.decision == AuthDecision.ACCEPT),
            wall0=wall0,
            metrics=metrics,
            tool_result_speak=tool_speak,
        )
        if tool_name2:
            tool_name = tool_name2
        metrics.tool_name = tool_name
        metrics.llm = llm_bundle.get("primary")
        metrics.llm_calls = llm_bundle.get("calls", [])
        metrics.llm_ms = llm_bundle.get("wall_ms")
        if llm_bundle.get("tool_ms"):
            metrics.tool_ms = (metrics.tool_ms or 0) + float(llm_bundle["tool_ms"])
        if llm_bundle.get("ttfb_ms") is not None:
            metrics.ttfb_ms = llm_bundle["ttfb_ms"]
        if llm_bundle.get("ttfa_ms") is not None:
            metrics.ttfa_ms = llm_bundle["ttfa_ms"]
        if llm_bundle.get("overlap_ms") is not None:
            metrics.overlap_ms = llm_bundle["overlap_ms"]

        metrics.tts_ms = llm_bundle.get("tts_ms")
        metrics.tts_synth_ms = llm_bundle.get("tts_synth_ms")
        metrics.tts_play_ms = llm_bundle.get("tts_play_ms")

        if speak_text:
            print(f"\nAssistant: {speak_text}")
            self.history.add(user_text, speak_text)

        if self._barge_in_flag or self.abort_event.is_set():
            metrics.barge_in = True
            metrics.notes.append("barge_in")

        metrics.total_latency_ms = (time.perf_counter() - wall0) * 1000
        if record_session:
            self.session.add(metrics)
        return TurnResult(user_text, speak_text, metrics, auth.decision.value, tool_name)

    def _emit_validated_clause(self, clause: str, gid: int, state: dict) -> None:
        """Atomic validated unit → UI + TTS (never raw token fragments)."""
        if self.abort_event.is_set():
            return
        if not self.gen_guard.should_enqueue_pcm(self.gen_guard.generation_id):
            return
        if self.tts.enabled and gid != self.tts.get_current_gen_id():
            return
        contract = (
            validate_spoken_unit(clause, generation_id=gid)
            if self.validate_before_speak
            else {"ok": True, "speak": clause, "fallback": False}
        )
        speak = sanitize_for_tts(str(contract.get("speak") or ""), fallback="")
        if not speak:
            return
        if self.on_validated_clause is not None:
            try:
                self.on_validated_clause(speak)
            except Exception:
                pass
        elif self.on_token is not None:
            try:
                self.on_token(speak)
            except Exception:
                pass
        if self.tts.enabled and self.stream_tts:
            self.tts.queue_text(speak, gid)
            state["first_chunk_sent"] = True
            state["spoken_parts"].append(speak)
            if speak[-1:] in ".?!":
                state["hit_sentence_end"] = True

    def _stream_llm_to_tts(
        self,
        user_text: str,
        wall0: float,
        *,
        tool_result_speak: str | None = None,
    ) -> tuple[str, dict]:
        """Token stream → validate complete clauses → same unit to UI + Piper."""
        buf = SentenceBuffer(
            first_chunk_min_chars=self.first_chunk_min_chars,
            voice_one_sentence=self.voice_one_sentence,
        )
        gid = self.tts.begin_turn() if self.tts.enabled else self.active_generation_id
        state = {
            "sentence_buffer": "",
            "first_chunk_sent": False,
            "spoken_parts": [],
            "hit_sentence_end": False,
            "generation_id": gid,
        }
        llm_wall = Timer()
        ttfb_ms = None
        decode_end = None

        def token_cb(chunk: str):
            if self.abort_event.is_set():
                buf.stop_generation = True
                return
            if not self.stream_tts or not self.tts.enabled:
                buf.feed(chunk)
                return
            state["sentence_buffer"] += chunk
            while True:
                clause, rest = first_complete_clause(
                    state["sentence_buffer"],
                    min_chars=self.first_chunk_min_chars,
                )
                if clause is None:
                    break
                state["sentence_buffer"] = rest
                self._emit_validated_clause(clause, gid, state)
                if self.voice_one_sentence and state["hit_sentence_end"]:
                    buf.stop_generation = True
                    buf.sentences_emitted = max(1, buf.sentences_emitted)
                    break

        def _gen():
            return self.llm.generate(
                self._messages(user_text, tool_result_speak=tool_result_speak),
                abort_callback=self.abort_event.is_set,
                should_stop=(lambda: buf.stop_generation) if self.voice_one_sentence else None,
                token_callback=token_cb,
                quiet=True,
            )

        try:
            raw, inf = self._with_genai(_gen)
            decode_end = time.perf_counter()
        except Exception as exc:  # noqa: BLE001 -- HailoRT can raise several
            # exception types (HailoRTTimeout, HailoRTStatusException, ...);
            # any of them previously propagated all the way out of the
            # realtime-turn thread uncaught, leaving the FSM context stuck
            # with terminal=None forever (no response.done, no spoken
            # feedback -- the turn just vanished). Fail closed instead:
            # speak once if nothing was said yet, then let the turn complete
            # normally so the FSM/UI recover for the next turn.
            decode_end = time.perf_counter()
            print(f"[llm] generation failed ({type(exc).__name__}: {exc}); failing turn closed")
            if not state["spoken_parts"]:
                fallback = str(validate_spoken_unit("")["speak"])
                if self.tts.enabled:
                    self.tts.enqueue(fallback, gid)
                    state["spoken_parts"].append(fallback)
            raw = " ".join(state["spoken_parts"])
            inf = None
        if inf is not None and inf.ttft_ms is not None and inf.first_token_time is not None:
            ttfb_ms = (inf.first_token_time - wall0) * 1000

        # Flush remainder as one validated clause
        if self.tts.enabled and self.stream_tts:
            rem = state["sentence_buffer"].strip()
            if rem and not self.abort_event.is_set():
                if not (self.voice_one_sentence and state["hit_sentence_end"]):
                    self._emit_validated_clause(rem, gid, state)
        else:
            for piece in buf.flush():
                if self.tts.enabled:
                    contract = validate_spoken_unit(piece) if self.validate_before_speak else {
                        "speak": piece
                    }
                    self.tts.enqueue(str(contract.get("speak") or piece), gid)

        if self.tts.enabled and self.wait_tts_drain:
            self.tts.wait_drain(timeout=60)

        snap = self.tts.timing_snapshot() if self.tts.enabled else {}
        ttfa_ms = None
        if snap.get("ttfa_wall") is not None:
            ttfa_ms = (snap["ttfa_wall"] - wall0) * 1000

        overlap_ms = None
        if snap.get("ttfa_wall") is not None and decode_end is not None:
            overlap_ms = max(0.0, (decode_end - snap["ttfa_wall"]) * 1000)

        if state["spoken_parts"]:
            speak = " ".join(state["spoken_parts"]).strip()
        else:
            speak = _clean_speak_text(raw)
            if self.validate_before_speak:
                speak = str(validate_spoken_unit(speak).get("speak") or speak)
        if self.voice_one_sentence and speak:
            for d in ".?!":
                if d in speak:
                    speak = speak.split(d)[0] + d
                    break

        return speak, {
            "primary": inf.to_dict() if inf is not None else {"error": "generation_failed"},
            "calls": [inf.to_dict()] if inf is not None else [],
            "wall_ms": llm_wall.ms(),
            "tool_ms": 0.0,
            "ttfb_ms": ttfb_ms,
            "ttfa_ms": ttfa_ms,
            "overlap_ms": overlap_ms,
            "tts_ms": snap.get("tts_ms"),
            "tts_synth_ms": snap.get("tts_synth_ms"),
            "tts_play_ms": snap.get("tts_play_ms"),
        }

    def _agent_loop(
        self,
        user_text: str,
        auth_accepted: bool,
        wall0: float,
        metrics: TurnMetrics,
        tool_result_speak: str | None = None,
    ) -> tuple[str, str | None, dict]:
        # Streaming path when tools are off (OEM default) or allowlist already ran
        if not self.tools_enabled:
            speak, bundle = self._stream_llm_to_tts(
                user_text, wall0, tool_result_speak=tool_result_speak
            )
            return speak, None, bundle

        calls = []
        tool_name = None
        tool_ms_total = 0.0
        llm_wall = Timer()
        ttfb_ms = None

        def _gen1():
            return self.llm.generate(
                self._messages(user_text, tool_result_speak=tool_result_speak),
                abort_callback=self.abort_event.is_set,
                quiet=True,
            )

        raw, inf = self._with_genai(_gen1)
        calls.append(inf.to_dict())
        if ttfb_ms is None and inf.ttft_ms is not None:
            ttfb_ms = (inf.first_token_time - wall0) * 1000 if inf.first_token_time else None

        rounds = self.multi_tool_rounds
        for _ in range(rounds):
            call = self.llm.parse_tool(raw)
            if call is None:
                break

            name = call.get("name", "")
            args = call.get("arguments") or {}
            tool_name = name
            print(f"[tool] {name}({args})")

            if looks_like_payment_tool(name) and not auth_accepted:
                result = {
                    "ok": False,
                    "error": "Payment not authorized. User must confirm.",
                    "speak": "I still need your confirmation before sending a payment.",
                }
            else:
                tt = Timer()
                result = execute_tool(name, args, self.tools)
                tool_ms_total += tt.ms()
                print(f"[tool result] {result}")

            if result.get("speak"):
                speak = str(validate_spoken_unit(str(result["speak"])).get("speak") or "")
                gid = self.tts.begin_turn()
                self.tts.enqueue(speak, gid)
                if self.wait_tts_drain:
                    self.tts.wait_drain(timeout=60)
                snap = self.tts.timing_snapshot()
                ttfa_ms = None
                if snap.get("ttfa_wall") is not None:
                    ttfa_ms = (snap["ttfa_wall"] - wall0) * 1000
                return (
                    speak,
                    tool_name,
                    {
                        "primary": calls[0] if calls else None,
                        "calls": calls,
                        "wall_ms": llm_wall.ms(),
                        "tool_ms": tool_ms_total,
                        "ttfb_ms": ttfb_ms,
                        "ttfa_ms": ttfa_ms,
                        "tts_ms": snap.get("tts_ms"),
                        "tts_synth_ms": snap.get("tts_synth_ms"),
                        "tts_play_ms": snap.get("tts_play_ms"),
                    },
                )

            feed = [
                {
                    "role": "user",
                    "content": (
                        f"Tool {name} returned: {result}. "
                        "Respond to the user in one short spoken sentence. Do not call tools."
                    ),
                }
            ]

            def _gen2(feed=feed):
                return self.llm.generate(
                    feed, abort_callback=self.abort_event.is_set, quiet=True
                )

            raw, inf2 = self._with_genai(_gen2)
            calls.append(inf2.to_dict())

        text = _clean_speak_text(raw)
        text = str(validate_spoken_unit(text).get("speak") or text)
        gid = self.tts.begin_turn()
        self.tts.enqueue(text, gid)
        if self.wait_tts_drain:
            self.tts.wait_drain(timeout=60)
        snap = self.tts.timing_snapshot()
        ttfa_ms = None
        if snap.get("ttfa_wall") is not None:
            ttfa_ms = (snap["ttfa_wall"] - wall0) * 1000
        return (
            text,
            tool_name,
            {
                "primary": calls[0] if calls else None,
                "calls": calls,
                "wall_ms": llm_wall.ms(),
                "tool_ms": tool_ms_total,
                "ttfb_ms": ttfb_ms,
                "ttfa_ms": ttfa_ms,
                "tts_ms": snap.get("tts_ms"),
                "tts_synth_ms": snap.get("tts_synth_ms"),
                "tts_play_ms": snap.get("tts_play_ms"),
            },
        )
