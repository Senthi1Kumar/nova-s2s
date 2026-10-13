"""Bounded accepted-history for multi-turn OEM conversations.

Callers: nova_hailo.pipeline.NovaPipeline._messages / run_text_turn.
Schema: HistoryTurn{user,assistant,tool_summary}; messages list[{role,content}]
"""
from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from nova_hailo.local_clock import format_clock_stamp

# Proper-noun-ish tokens. Sentence-initial function words are excluded so a
# reply merely *starting* with "It" or "The" is not treated as naming an entity.
_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9&.\-]{2,}\b")
_ENTITY_STOPWORDS = frozenset(
    {
        "The", "This", "That", "There", "These", "Those", "Then",
        "It", "Its", "You", "Your", "Yes", "Yeah", "Hey", "Hello",
        "We", "Our", "They", "Their", "She", "Her", "His", "Him",
        "What", "When", "Where", "Why", "How", "Who", "Which",
        "And", "But", "For", "Not", "Sure", "Okay", "Sorry", "Uh", "Um",
        "Right", "Currently", "Here", "Now", "About", "All", "Let",
    }
)


def _entities(text: str) -> set[str]:
    return {m.group(0) for m in _ENTITY_RE.finditer(text or "")} - _ENTITY_STOPWORDS


def _leads_with_possessive(reply: str, carried: set[str]) -> bool:
    """True when the reply OPENS by claiming something about a carried entity.

    The measured bleed was a wrong answer re-subjecting itself turn after turn:
    "NVIDIA's stock price..." became "NVIDIA's vehicle status...", then
    "...windows...", "...lights...". That shape -- entity in the possessive, at
    the head of the sentence -- is the thing worth rejecting.

    Merely mentioning a recurring proper noun is NOT bleed. An assistant named
    Nova says "Nova" often, and a genuine follow-up ("tell me more" ->
    "Mahindra also launched...") reuses the topic entity by design. Rejecting
    those emptied the history and left the model with no context to advance on.
    """
    head = (reply or "").lstrip()[:60]
    return any(head.startswith((f"{e}'s", f"{e}’s")) for e in carried)


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

    def __init__(
        self,
        max_turns: int = 2,
        *,
        unanswered_ttl_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_turns = max(0, int(max_turns))
        self.unanswered_ttl_s = float(unanswered_ttl_s)
        self._clock = clock
        self._unanswered_at = 0.0
        self._turns: deque[HistoryTurn] = deque(maxlen=self.max_turns or 1)
        if self.max_turns <= 0:
            self._turns = deque(maxlen=0)
        # Question whose reply was dropped as a refusal; replayed once.
        self._unanswered: str | None = None

    def clear(self) -> None:
        self._turns.clear()
        self._unanswered = None

    def add(
        self,
        user: str,
        assistant: str,
        *,
        tool_summary: str | None = None,
        trusted: bool = True,
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
            # Drop the reply, keep the question: without it the next turn's
            # "Why is it?" / "search for it" has nothing to refer to. Not when
            # the "question" may have been Nova's own voice heard back.
            if trusted:
                self._unanswered = u[:240]
                self._unanswered_at = self._clock()
            return
        # Reject a reply that carries an entity forward from the previous reply
        # when the current question never mentioned it. A wrong "NVIDIA's stock
        # price..." answer otherwise mutated into "NVIDIA's vehicle status...",
        # "...windows...", "...lights..." across later unrelated turns: none of
        # those are refusals, so the filter above never caught them, and each
        # bled reply seeded the next.
        if self._turns:
            carried = _entities(a) & _entities(self._turns[-1].assistant)
            if _leads_with_possessive(a, carried - _entities(u)):
                return
        # A "summary" that merely repeats the answer is not evidence -- it is
        # the same sentence stored twice. build_messages replays it in the USER
        # channel, where it reads as material supplied for the *next* question:
        # a search about a new phone was followed by an unrelated election
        # question, and the model answered about the phone.
        ts = (tool_summary or "").strip()
        if ts and _norm(ts)[:120] in _norm(a):
            ts = ""
        self._turns.append(
            HistoryTurn(user=u[:240], assistant=a[:240], tool_summary=(ts or None))
        )
        self._unanswered = None

    def build_messages(
        self,
        system_prompt: str,
        user_text: str,
        *,
        tool_result_speak: str | None = None,
        no_think: bool = False,
        now: datetime | None = None,
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
        # Stable facts belong beside the question, not inferred from the
        # transcript. Without the date, a calendar answer drifted a day between
        # two turns and then contradicted itself. It rides on the *current* user
        # turn rather than the system prompt on purpose: a changing value in the
        # prefix would break the provider's prompt cache on every request, and
        # recency also keeps it from being lost in the middle of a long context.
        stamp = format_clock_stamp(now)
        if ut and self._unanswered:
            # Beside the question, like the clock: context for resolving "it",
            # not a stored exchange the model could echo back. Offered once, and
            # only while it is recent.
            if self._clock() - self._unanswered_at <= self.unanswered_ttl_s:
                ut = f"(Earlier question you could not answer: {self._unanswered})\n{ut}"
            self._unanswered = None
        ut = f"(Right now it is {stamp}.)\n{ut}" if ut else ut
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

    def pairs(self) -> list[dict[str, str]]:
        """Recent user and assistant lines, oldest first."""
        return [{"user": turn.user, "assistant": turn.assistant} for turn in self._turns]
