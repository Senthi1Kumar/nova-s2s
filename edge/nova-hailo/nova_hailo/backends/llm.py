"""Hailo GenAI LLM wrapper (hailo_platform.genai.LLM)."""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import redirect_stdout
from io import StringIO

from hailo_platform import VDevice
from hailo_platform.genai import LLM

try:
    from hailo_apps.python.core.common.hailo_logger import get_logger
    from hailo_apps.python.gen_ai_apps.gen_ai_utils.llm_utils import tool_parsing
except ImportError:
    import logging

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
