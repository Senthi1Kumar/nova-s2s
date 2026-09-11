"""Repair proper nouns the ASR reliably mishears, before they reach the LLM.

A wrong proper noun does not degrade gracefully -- it becomes a wrong search
query, and the wrong answer that comes back anchors the rest of the session.
Observed live: "BRICS summit" transcribed as "Bricks summit" returned a
masonry-industry event ("the Red Brick Forum in Jeddah"), and the next three
turns built on it.

Corrections are matched as PHRASES, not bare words. "bricks" is an ordinary
English word and rewriting it everywhere would break "a wall of bricks"; only
"bricks summit" is unambiguous. Keep that discipline when adding entries: if a
pattern could fire on innocent speech, it needs more context, not more hope.
"""
from __future__ import annotations

import re

# (pattern, replacement). Case-insensitive, word-bounded.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pat, re.I), rep)
    for pat, rep in (
        # BRICS -- the live failure. Only corrected beside a qualifier.
        (r"\b(?:bricks|brix|brig'?s|briggs|breaks)\s+(summit|summits)\b", r"BRICS \1"),
        (
            r"\b(?:bricks|brix|brig'?s|briggs)\s+"
            r"(nations?|countries|bloc|group|members?|leaders?)\b",
            r"BRICS \1",
        ),
        # Marques this is being demonstrated to.
        (r"\b(?:porsch|porshe|porsha|porche)\b", "Porsche"),
        (r"\b(?:folks\s*wagon|volks\s*wagon|folkswagen|wolkswagen)\b", "Volkswagen"),
        # Silicon the project actually runs on.
        (r"\b(?:snima|sigma\s*ai|cima\s*ai)\b", "SiMa.ai"),
        (r"\bhailow\b", "Hailo"),
    )
)


def correct(text: str) -> str:
    """Apply phrase-level proper-noun repairs; unchanged when nothing matches.

    Cheap enough to run on every transcript: a handful of compiled patterns
    over one short utterance.
    """
    if not text:
        return text
    out = text
    for pattern, replacement in _RULES:
        out = pattern.sub(replacement, out)
    return out
