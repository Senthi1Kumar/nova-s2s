"""User-facing speak: keep conversational fillers; never TTS tool JSON."""
from __future__ import annotations

import re

# Conversational particles vui-style (Inflect speaks them as words).
FILLERS = ("um", "uh", "hmm", "yeah", "ah", "oh", "well")

_JSON_BLOCK = re.compile(r"```(?:json)?\s*\{.*?}\s*```", re.I | re.S)
_BARE_OBJ = re.compile(
    r'\{\s*["\']?(?:type|action|tool|name)["\']?\s*:',
    re.I,
)
_FUNC_CALL = re.compile(r"\bfunction[_\s-]?call\b|<tool_call>", re.I)


def sanitize_for_tts(text: str, *, fallback: str = "I'm here.") -> str:
    """Strip tool JSON / schemas; keep filler words for human-like speech."""
    t = (text or "").strip()
    if not t:
        return fallback
    t = _JSON_BLOCK.sub(" ", t)
    if _BARE_OBJ.search(t) or _FUNC_CALL.search(t):
        # Drop the JSON-looking span; keep leading prose if any.
        t = re.split(r"\{", t, maxsplit=1)[0].strip()
    t = re.sub(r"\s+", " ", t).strip()
    if not t or not any(c.isalpha() for c in t):
        return fallback
    return t


def has_filler(text: str) -> bool:
    tokens = re.findall(r"[a-z']+", (text or "").lower())
    return any(w in tokens for w in FILLERS)
