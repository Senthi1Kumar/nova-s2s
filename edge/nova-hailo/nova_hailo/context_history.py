"""Bounded accepted-history for multi-turn OEM conversations.

Callers: nova_hailo.pipeline.NovaPipeline._messages / run_text_turn.
Schema: HistoryTurn{user,assistant,tool_summary}; messages list[{role,content}]
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


def _norm(s: str) -> str:
    """Case/punctuation-insensitive key for duplicate-reply detection."""
    return "".join(c for c in (s or "").lower() if c.isalnum() or c.isspace()).strip()


_REFUSAL_MARKERS = (
    "cant assist",
    "cannot assist",
    "im sorry",
    "i dont understand",
    "not sure",
    "could you repeat",
    "didnt catch",
    # response_contract._FALLBACK's exact wording. Confirmed pipeline.py:412
    # stores contract["speak"] (== _FALLBACK on any hard contract failure,
    # including persona_drift) via history.add() -- without this marker the
    # fallback seeds the next prompt and re-creates a repetition/refusal
    # lock -- the model echoes its own last reply back turn after turn.
    "could you say that again",
)


def _is_refusal(norm_text: str) -> bool:
    """True for apology/fallback replies that must not seed the next prompt."""
    return any(m in norm_text for m in _REFUSAL_MARKERS)


@dataclass(frozen=True)
class HistoryTurn:
    user: str
    assistant: str
    tool_summary: str | None = None


class ConversationHistory:
    """Keep at most ``max_turns`` accepted user/assistant pairs."""

    def __init__(self, max_turns: int = 2):
        self.max_turns = max(0, int(max_turns))
        self._turns: deque[HistoryTurn] = deque(maxlen=self.max_turns or 1)
        if self.max_turns <= 0:
            self._turns = deque(maxlen=0)

    def clear(self) -> None:
        self._turns.clear()

    def add(
        self,
        user: str,
        assistant: str,
        *,
        tool_summary: str | None = None,
    ) -> None:
        u = (user or "").strip()
        a = (assistant or "").strip()
        if not u or not a or self.max_turns <= 0:
            return
        # Never store a near-duplicate assistant reply: identical replies in
        # history teach a 1.5B model to emit that same line for every later turn
        # (measured repetition lock, 2026-07-29). Check every retained turn, not
        # just the last — refusals alternate with other turns and still cascade.
        key = _norm(a)
        if any(_norm(t.assistant) == key for t in self._turns):
            return
        # Refusals/fallbacks are never worth remembering: keeping one makes the
        # model refuse the next (unrelated) turn too.
        if _is_refusal(key):
            return
        self._turns.append(
            HistoryTurn(user=u[:240], assistant=a[:240], tool_summary=(tool_summary or None))
        )

    def build_messages(
        self,
        system_prompt: str,
        user_text: str,
        *,
        tool_result_speak: str | None = None,
        no_think: bool = False,
    ) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        for turn in self._turns:
            messages.append({"role": "user", "content": turn.user})
            messages.append({"role": "assistant", "content": turn.assistant})
            if turn.tool_summary:
                messages.append(
                    {
                        "role": "user",
                        "content": f"(Prior tool result summary: {turn.tool_summary})",
                    }
                )
        ut = (user_text or "").strip()
        if no_think and "/no_think" not in ut.lower():
            ut = ut + "\n/no_think"
        if tool_result_speak:
            ut = (
                f"{ut}\n\nTool result for the user (speak from this, do not invent): "
                f"{tool_result_speak.strip()[:320]}"
            )
        messages.append({"role": "user", "content": ut})
        return messages

    def __len__(self) -> int:
        return len(self._turns)
