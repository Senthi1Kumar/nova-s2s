"""Hailo GenAI LLM wrapper (hailo_platform.genai.LLM)."""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from io import StringIO

from hailo_platform import VDevice
from hailo_platform.genai import LLM

try:
    from hailo_apps.python.core.common.defines import SHARED_VDEVICE_GROUP_ID
    from hailo_apps.python.core.common.hailo_logger import get_logger
    from hailo_apps.python.gen_ai_apps.gen_ai_utils.llm_utils import tool_parsing
except ImportError:
    import logging

    SHARED_VDEVICE_GROUP_ID = "SHARED"
    get_logger = lambda n: logging.getLogger(n)  # noqa: E731
    tool_parsing = None

from nova_hailo.metrics import InferenceMetrics

logger = get_logger(__name__)

# Llama-3 / Qwen chat control tokens (and mangled spaced variants from bad HEF decode)
# Callers: HailoLLM.generate.
_SPECIAL_TOKEN_RE = re.compile(
    r"<\|[^|>]{0,40}\|>|"
    r"<\|[^|>]{0,40}\|"
    r"|</?s>|"
    r"<｜[^｜]{0,40}｜>",
    re.I,
)
_HEADER_LOOP_RE = re.compile(r"start\s*header|end\s*header|eot_id|im_end|im_start", re.I)


def sanitize_llm_token(token: str) -> str:
    """Strip special tokens from a streamed piece; empty if pure control junk."""
    if not token:
        return ""
    if _HEADER_LOOP_RE.search(token) and "<" in token:
        return ""
    t = _SPECIAL_TOKEN_RE.sub("", token)
    t = re.sub(r"(?i)\b(start|end)\s*header\s*id\b\|?", " ", t)
    t = t.replace("<|", " ").replace("|>", " ")
    return t


def sanitize_llm_text(text: str) -> str:
    """Post-pass: drop control-token storms; keep spoken words if any remain."""
    if not text:
        return ""
    header_hits = len(_HEADER_LOOP_RE.findall(text))
    # Llama HEF token storms: discard entire utterance (don't TTS "1 assistant")
    if header_hits >= 2:
        return ""
    t = _SPECIAL_TOKEN_RE.sub(" ", text)
    t = re.sub(r"(?i)(<\|)?\s*(start|end)\s*header\s*id\s*\|?", " ", t)
    t = re.sub(r"<\|[^>]{0,80}", " ", t)
    letters = sum(1 for c in t if c.isalpha())
    junk = sum(1 for c in t if c in "<>|~")
    if letters < 3 or junk > letters:
        return ""
    return re.sub(r"\s+", " ", t).strip()


def _is_control_token(token: str) -> bool:
    t = (token or "").strip()
    if not t:
        return False
    if t in {
        "<|im_end|>",
        "<|eot_id|>",
        "<|endoftext|>",
        "<|end_header_id|>",
        "<|start_header_id|>",
    }:
        return True
    if _HEADER_LOOP_RE.search(t) and ("<" in t or "|" in t):
        return True
    return False


def _parse_tool_fallback(raw: str) -> dict | None:
    m = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", raw or "", re.DOTALL)
    if not m:
        return None
    try:
        call = json.loads(m.group(1))
        if "name" in call:
            if "arguments" not in call:
                call["arguments"] = {}
            return call
    except json.JSONDecodeError:
        return None
    return None


def _estimate_prompt_tokens(messages: list[dict]) -> int:
    """Rough estimate: chars/4 (not word count)."""
    total_chars = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict):
                    total_chars += len(str(c.get("text", "")))
                else:
                    total_chars += len(str(c))
        else:
            total_chars += len(str(content))
    return max(1, total_chars // 4)


class HailoLLM:
    """Streaming-capable Hailo GenAI LLM."""

    def __init__(
        self,
        vdevice: VDevice,
        hef_path: str,
        temperature: float = 0.15,
        seed: int = 42,
        max_tokens: int = 24,
        no_think: bool = False,
    ):
        logger.info("Loading LLM: %s", hef_path)
        print(f"Loading LLM: {hef_path}")
        self.llm = LLM(vdevice, hef_path)
        self.hef_path = hef_path
        self.temperature = temperature
        self.seed = seed
        self.max_tokens = max_tokens
        self.no_think = no_think
        self.last_metrics: InferenceMetrics | None = None

    def generate(
        self,
        messages: list[dict],
        max_tokens: int | None = None,
        token_callback: Callable[[str], None] | None = None,
        abort_callback: Callable[[], bool] | None = None,
        should_stop: Callable[[], bool] | None = None,
        quiet: bool = True,
    ) -> tuple[str, InferenceMetrics]:
        prompt_est = _estimate_prompt_tokens(messages)
        metrics = InferenceMetrics()
        metrics.no_think = self.no_think
        metrics.device = "hailo10h_npu"
        metrics.start(prompt_tokens_est=prompt_est)
        parts: list[str] = []
        idx = 0
        cap = max_tokens if max_tokens is not None else self.max_tokens

        def _run():
            nonlocal idx
            with self.llm.generate(
                prompt=messages,
                temperature=self.temperature,
                seed=self.seed,
                max_generated_tokens=cap,
            ) as gen:
                for token in gen:
                    if abort_callback and abort_callback():
                        break
                    if should_stop and should_stop():
                        break
                    if self.no_think and ("<think>" in token or token.strip() == "<think>"):
                        break
                    if _is_control_token(token):
                        # Llama HEF often enters <|start_header_id|> loops — stop
                        if _HEADER_LOOP_RE.search(token):
                            break
                        continue
                    cleaned = sanitize_llm_token(token)
                    if not cleaned:
                        # Consecutive junk after some text → stop
                        if parts and _HEADER_LOOP_RE.search(token or ""):
                            break
                        continue
                    parts.append(cleaned)
                    metrics.record_token(idx)
                    idx += 1
                    if token_callback:
                        token_callback(cleaned)
                    if idx >= cap:
                        break
            metrics.end()

        if quiet:
            with redirect_stdout(StringIO()):
                _run()
        else:
            _run()

        text = sanitize_llm_text("".join(parts))
        self.last_metrics = metrics
        return text, metrics

    def parse_tool(self, raw: str):
        if tool_parsing is None:
            return _parse_tool_fallback(raw)
        return tool_parsing.parse_function_call(raw)

    def clear(self):
        try:
            self.llm.clear_context()
        except Exception:
            pass

    def release(self):
        self.clear()
        try:
            self.llm.release()
        except Exception:
            pass


class HailoLLMCpp:
    """Opt-in native LLM backend: same public interface as HailoLLM, but the
    blocking NPU read loop runs through hailo_llm_cpp (csrc/hailo_llm_cpp.cpp,
    built via scripts/build_hailo_llm_cpp.sh) with the GIL explicitly released,
    instead of hailo_platform.genai's Python binding. Fixes GIL contention
    that starved the TTS worker thread during decode (ROADMAP.md #6d / Sprint
    1b) and, independently, drops cold TTFT ~942ms -> ~320ms by skipping
    whatever marshalling overhead the Python binding adds.

    Creates its own native VDevice (cannot share nova_hailo's Python-side
    hailo_platform.VDevice) -- untested combined with stt_engine: whisper_hef,
    which also claims the device via the Python binding. Fine for the default
    parakeet (CPU) STT config.
    """

    def __init__(
        self,
        vdevice=None,  # accepted for HailoLLM interface parity; unused, see class docstring
        hef_path: str = "",
        temperature: float = 0.15,
        seed: int = 42,
        max_tokens: int = 24,
        no_think: bool = False,
    ):
        # Built .so lives next to this file (scripts/build_hailo_llm_cpp.sh);
        # not a normal package import path, so add it explicitly.
        backends_dir = os.path.dirname(os.path.abspath(__file__))
        if backends_dir not in sys.path:
            sys.path.insert(0, backends_dir)
        import hailo_llm_cpp  # noqa: PLC0415 -- optional native extension, Pi-only

        logger.info("Loading LLM (native C++ backend): %s", hef_path)
        print(f"Loading LLM (native C++ backend): {hef_path}")
        # group_id must match pipeline.py's Python-side VDevice group_id --
        # otherwise this VDevice competes for an exclusive physical device
        # slot instead of joining the existing shared one (only one Hailo-10H).
        self._llm = hailo_llm_cpp.HailoLLMCpp(
            hef_path, temperature, seed, max_tokens, SHARED_VDEVICE_GROUP_ID
        )
        self.hef_path = hef_path
        self.temperature = temperature
        self.seed = seed
        self.max_tokens = max_tokens
        self.no_think = no_think
        self.last_metrics: InferenceMetrics | None = None

    def generate(
        self,
        messages: list[dict],
        max_tokens: int | None = None,
        token_callback: Callable[[str], None] | None = None,
        abort_callback: Callable[[], bool] | None = None,
        should_stop: Callable[[], bool] | None = None,
        quiet: bool = True,
    ) -> tuple[str, InferenceMetrics]:
        prompt_est = _estimate_prompt_tokens(messages)
        metrics = InferenceMetrics()
        metrics.no_think = self.no_think
        metrics.device = "hailo10h_npu"
        metrics.start(prompt_tokens_est=prompt_est)
        parts: list[str] = []
        idx = 0
        cap = max_tokens if max_tokens is not None else self.max_tokens
        stop_flag = {"stop": False}

        prompt_json = [json.dumps(m) for m in messages]

        def _token_cb(token: str):
            nonlocal idx
            if stop_flag["stop"]:
                return
            if self.no_think and ("<think>" in token or token.strip() == "<think>"):
                stop_flag["stop"] = True
                return
            if _is_control_token(token):
                if _HEADER_LOOP_RE.search(token):
                    stop_flag["stop"] = True
                return
            cleaned = sanitize_llm_token(token)
            if not cleaned:
                if parts and _HEADER_LOOP_RE.search(token or ""):
                    stop_flag["stop"] = True
                return
            parts.append(cleaned)
            metrics.record_token(idx)
            idx += 1
            if token_callback:
                token_callback(cleaned)
            if idx >= cap:
                stop_flag["stop"] = True

        def _should_stop() -> bool:
            if stop_flag["stop"]:
                return True
            if abort_callback and abort_callback():
                return True
            if should_stop and should_stop():
                return True
            return False

        def _run():
            return self._llm.generate(prompt_json, cap, _token_cb, _should_stop)

        if quiet:
            with redirect_stdout(StringIO()):
                raw = _run()
        else:
            raw = _run()

        metrics.end()
        text = sanitize_llm_text("".join(parts)) if parts else sanitize_llm_text(raw)
        self.last_metrics = metrics
        return text, metrics

    def parse_tool(self, raw: str):
        if tool_parsing is None:
            return _parse_tool_fallback(raw)
        return tool_parsing.parse_function_call(raw)

    def clear(self):
        try:
            self._llm.clear_context()
        except Exception:
            pass

    def release(self):
        self.clear()
