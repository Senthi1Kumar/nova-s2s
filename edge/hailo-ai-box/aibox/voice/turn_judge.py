"""System One-style semantic judgments with deterministic and local D3 backends."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Protocol


class TurnQuestions:
    @staticmethod
    def all() -> dict[str, dict[str, Any]]:
        return {
            "turn_complete": {
                "type": "noul",
                "instructions": "Has the user completed their thought and yielded the floor to the assistant?",
            },
            "holding_floor": {
                "type": "noul",
                "instructions": "Is the user unfinished or explicitly asking for more time before the assistant responds?",
            },
            "addressed_to_agent": {
                "type": "noul",
                "instructions": "Is the latest speech intended for the assistant rather than another person or device?",
            },
            "taking_floor": {
                "type": "noul",
                "instructions": "While the assistant is speaking, is the user genuinely interrupting rather than giving a backchannel?",
            },
        }


class TurnJudge(Protocol):
    async def judge(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, float]: ...


def _noul(result: Any, name: str) -> float:
    answer = result.get("answers", result)[name]
    if isinstance(answer, dict):
        return float(answer.get("noul", 0.0))
    return float(answer)


class D3EdgeJudge:
    def __init__(self, model_id: str = "vllm-sr/d3-edge") -> None:
        self.model_id = model_id
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from transformers import AutoModel

            self._model = AutoModel.from_pretrained(self.model_id, trust_remote_code=True)
        return self._model

    async def judge(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, float]:
        result = await asyncio.to_thread(self._load().system_one, state=state, questions=questions)
        return {name: _noul(result, name) for name in questions}


class TimeoutJudge:
    async def judge(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, float]:
        text = str(state.get("partial_transcript", "")).lower().strip()
        words = set(re.findall(r"[a-z']+", text))
        backchannel = {"right", "okay", "ok", "yeah", "yes", "sure", "mm", "mhm", "thanks"}
        stopping = {"wait", "stop", "cancel", "pause", "hold"}
        holding = any(phrase in text for phrase in (
            "hold on", "one moment", "let me", "give me a second", "just a second",
        ))
        side_talk = any(phrase in text for phrase in (
            "hold my coffee", "not for you", "talking to", "can you take this",
        ))
        ordered_words = re.findall(r"[a-z']+", text)
        last_word = ordered_words[-1] if ordered_words else ""
        unfinished = last_word in {
            "and", "or", "but", "so", "because", "to", "for", "with", "the", "a", "an",
            "is", "are", "was", "were", "what", "want", "know", "tell", "of",
        } and not text.endswith(("?", ".", "!"))
        taking = 0.95 if words & stopping else (0.05 if words and words <= backchannel else 0.5)
        return {
            "turn_complete": 0.1 if holding or unfinished else 0.9,
            "holding_floor": 0.95 if holding else (0.75 if unfinished else 0.05),
            "addressed_to_agent": 0.1 if side_talk else 0.85,
            "taking_floor": taking,
        }


def build_turn_judge(settings: Any) -> TurnJudge:
    backend = str(getattr(settings, "turn_judge_backend", "timeout")).strip().lower()
    if backend == "d3_edge":
        return D3EdgeJudge(getattr(settings, "d3_model_id", "vllm-sr/d3-edge"))
    if backend != "timeout":
        raise ValueError(f"unknown turn judge backend: {backend}")
    return TimeoutJudge()
