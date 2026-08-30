"""Short, honest spoken filler for the gap between "model picked a tool" and
"tool result comes back".

On a tool-using turn the OpenRouter round trip is: LLM call #1 decides to
call a tool (~800ms to first token), the tool runs (~900ms for web_search),
then LLM call #2 composes the spoken answer (~1700ms). Nothing is spoken
until the very end, so the user hears silence for the whole turn. Speaking a
line here as soon as the tool is chosen -- while the tool runs in the
background -- moves first audio from the end of the turn to right after the
tool-dispatch decision.

Callers: nova_hailo.pipeline._openrouter_turn (the only caller). Pure lookup
and guard logic only -- no TTS or pipeline coupling, so both are testable
without hailo_platform/hardware.
"""
from __future__ import annotations

import random
from collections.abc import Sequence

# Every line stays at or under ~5 words so the ack never competes with the
# real answer that follows. Several variants per tool so back-to-back tool
# turns don't sound like a recording -- see choose_tool_ack().
_TOOL_ACK_LINES: dict[str, tuple[str, ...]] = {
    "web_search": (
        "Let me look that up.",
        "One sec, checking.",
        "Let me check that.",
    ),
    "check_calendar": (
        "Checking your calendar.",
        "One sec, calendar check.",
        "Let me check your calendar.",
    ),
    "check_email": (
        "Checking your mail.",
        "One sec, checking mail.",
        "Let me check your inbox.",
    ),
    "list_drive_files": (
        "Checking your files.",
        "One sec, checking files.",
        "Let me check your drive.",
    ),
    # deep_research kicks off a longer async job (Tavily), not a quick
    # fetch, so this sets a longer-wait expectation instead of promising
    # something that lands in under a second.
    "deep_research": (
        "This will take a bit.",
        "Let me dig into that.",
        "This may take a moment.",
    ),
}
_DEFAULT_ACK_LINES: tuple[str, ...] = (
    "One moment.",
    "Let me check.",
    "Working on it.",
)


def choose_tool_ack(tool_name: str | None, rng: random.Random | None = None) -> str:
    """Pick a short filler line for ``tool_name``.

    Deterministic given ``rng``: pass a seeded ``random.Random(seed)`` for
    reproducible tests. Without ``rng`` this draws from the shared ``random``
    module, which is deliberate -- a fixed line every time would read as a
    canned recording. Unknown/unmapped tool names fall back to a generic
    line rather than raising, since new tools may be added to the allowlist
    without this table being updated first.
    """
    key = (tool_name or "").strip().lower()
    lines = _TOOL_ACK_LINES.get(key, _DEFAULT_ACK_LINES)
    chooser = rng if rng is not None else random
    return chooser.choice(lines)


def should_speak_tool_ack(*, enabled: bool, round_index: int, spoken_parts: Sequence) -> bool:
    """True only for the round that first dispatches a tool.

    ``round_index`` is 0 for the round in which the model decides to call a
    tool; any later round is a follow-up after a tool result already came
    back, and must never get a second ack. ``spoken_parts`` is that round's
    own accumulated spoken clauses -- non-empty means the model already
    streamed real content (via token_cb) in this same round, so speaking a
    filler on top of it would double-speak.
    """
    return bool(enabled) and round_index == 0 and not spoken_parts
