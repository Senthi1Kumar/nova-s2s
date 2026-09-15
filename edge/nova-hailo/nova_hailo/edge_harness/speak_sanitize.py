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

# Cloud models answer research questions in markdown, and it reached the
# synthesizer verbatim -- a live turn spoke '**Paper title:** "Hexagon-MLIR...'
# with the asterisks intact. Piper has no idea these are formatting, so they
# surface as mangled prosody or literal punctuation.
_MD_FENCE = re.compile(r"```.*?```", re.S)
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")             # ![alt](url) -> alt
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")               # [text](url) -> text
_MD_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3})(\S.*?\S|\S)\1")  # **b** _i_ ***bi***
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_MD_BULLET = re.compile(r"^\s{0,4}(?:[-*+]|\d{1,2}[.)])\s+", re.M)
_MD_RULE = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$", re.M)
_MD_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)

# Typographic characters Piper mispronounces or reads as noise. Curly quotes
# and the non-breaking hyphen (U+2011) both appeared in live replies.
_UNICODE_FIXES = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "‑": "-", "–": "-", "—": "-", "…": "...",
    " ": " ", "​": "",
}


def strip_markdown(text: str) -> str:
    """Turn markdown into plain prose a speech synthesizer can read.

    Order matters: images before links (both open with a bracket), fences
    before inline code, and emphasis after links so a bolded link keeps its
    label. Structural markers become spaces rather than vanishing, so a bullet
    never fuses onto the previous line.
    """
    t = _MD_FENCE.sub(" ", text)
    t = _MD_IMAGE.sub(r"\1", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _MD_RULE.sub(" ", t)
    t = _MD_HEADING.sub("", t)
    t = _MD_QUOTE.sub("", t)
    t = _MD_BULLET.sub("", t)
    t = _MD_CODE.sub(r"\1", t)
    # Emphasis nests (***x***), so iterate; each pass strictly removes markers,
    # which bounds the loop.
    for _ in range(3):
        new = _MD_EMPHASIS.sub(r"\2", t)
        if new == t:
            break
        t = new
    for bad, good in _UNICODE_FIXES.items():
        t = t.replace(bad, good)
    return t


def sanitize_for_tts(text: str, *, fallback: str = "I'm here.") -> str:
    """Strip tool JSON / schemas and markdown; keep fillers for human speech."""
    t = (text or "").strip()
    if not t:
        return fallback
    t = _JSON_BLOCK.sub(" ", t)
    if _BARE_OBJ.search(t) or _FUNC_CALL.search(t):
        # Drop the JSON-looking span; keep leading prose if any.
        t = re.split(r"\{", t, maxsplit=1)[0].strip()
    t = strip_markdown(t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t or not any(c.isalpha() for c in t):
        return fallback
    return t


def has_filler(text: str) -> bool:
    tokens = re.findall(r"[a-z']+", (text or "").lower())
    return any(w in tokens for w in FILLERS)
