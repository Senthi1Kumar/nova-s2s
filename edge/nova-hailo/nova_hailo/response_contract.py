"""Production spoken-response contract for OEM demo.

Callers: nova_hailo.pipeline, nova_hailo.web.realtime_session.
Bench twin: nova_hailo.bench.contracts.response_contract_ok.
API: validate_spoken_unit(text)->{ok,failures,text,speak,fallback}; first_complete_clause(buf)->(clause,rest)
"""
from __future__ import annotations

import re
from typing import Any

_CONTROL_RE = re.compile(
    r"<\|[^|>]{0,40}\|>|"
    r"<\|[^|>]{0,40}\|"
    r"|</?s>|"
    r"<｜[^｜]{0,40}｜>|"
    r"</?think>|"
    r"```|"
    r"<tool_call>|"
    r"</tool_call>",
    re.I,
)
_HEADER_LOOP_RE = re.compile(r"start\s*header|end\s*header|eot_id|im_end|im_start", re.I)
# A list marker left stranded at the end of a truncated answer: " 2." or " 2)"
# preceded by a real sentence end. The lookbehind is what keeps "costs $5." and
# "back in 2024." safe -- those digits are not preceded by sentence punctuation.
_ORPHAN_LIST_MARKER_RE = re.compile(r"(?<=[.?!])\s+\d{1,2}[.)]\s*$")
_ROLE_LEAK_RE = re.compile(
    r"\b(system|assistant|user)\s*:\s|"
    r"^as an ai\b|"
    r"you are nova\b.*you are nova",
    re.I,
)
_UNSAFE_CLAIM_RE = re.compile(
    r"\b("
    r"payment sent|funds (moved|transferred)|i('ve| have) (sent|transferred)|"
    r"email sent|deleted (the )?event|created (the )?event"
    r")\b",
    re.I,
)
# Persona/identity drift: never legitimate from any path (chat, summarizer,
# future paths). Deliberately narrower than search_summarizer.py's Tier-B
# capability refusals ("can't provide", "no internet") -- those are only wrong
# on the search path where evidence was already fetched, and promoting them
# here would replace oem_tools.py's honest declines (e.g. "I can't check the
# weather yet.") with the generic fallback. See TODO.md P0.9.
_PERSONA_DRIFT_RE = re.compile(
    r"\b("
    r"as an ai|"
    r"language model|"
    r"i('m| am) not nova|"
    r"not nova anymore|"
    r"i am now the vehicle"
    r")\b",
    re.I,
)
_FALLBACK = "I'm here. Could you say that again?"


def validate_spoken_unit(
    text: str,
    *,
    max_chars: int = 220,
    forbid_tools: bool = True,
    generation_id: int | None = None,
) -> dict[str, Any]:
    """Return ok + sanitized text, or ok=False with fallback speak text."""
    t = (text or "").strip()
    failures: list[str] = []
    if not t:
        failures.append("empty")
    if len(t) > max_chars:
        failures.append("too_long")
        t = t[: max_chars - 1].rsplit(" ", 1)[0].strip() + "."
    if _CONTROL_RE.search(t) or _HEADER_LOOP_RE.search(t):
        failures.append("control_token")
    if _ROLE_LEAK_RE.search(t):
        failures.append("role_leak")
    if _PERSONA_DRIFT_RE.search(t):
        failures.append("persona_drift")
    if forbid_tools and _UNSAFE_CLAIM_RE.search(t):
        failures.append("unsafe_action_claim")
    letters = sum(1 for c in t if c.isalpha())
    junk = sum(1 for c in t if c in "<>|~`")
    if t and (letters < 3 or junk > letters):
        failures.append("malformed")
    # Never speak a fragment. When generation stops on the token cap it lands
    # mid-sentence ("...an example of what it might"), and TTS faithfully speaks
    # the stump. Trim back to the last complete sentence; only keep an unpunctuated
    # tail if there is no complete sentence to fall back to.
    cleaned = re.sub(r"\s+", " ", t).strip()
    # A numbered list truncated right after its marker ("... on a deal. 2.")
    # already ends in "." so the fragment check below skips it -- but the
    # dangling marker is not a sentence, and TTS reads it aloud as "two".
    # Only a bare 1-2 digit marker following a real sentence end is stripped,
    # so legitimate tails like "... costs $5." or "... back in 2024." survive.
    if _ORPHAN_LIST_MARKER_RE.search(cleaned):
        cleaned = _ORPHAN_LIST_MARKER_RE.sub("", cleaned).strip()
        failures.append("trimmed_incomplete_sentence")
    if cleaned and cleaned[-1] not in ".?!":
        cut = max(cleaned.rfind(c) for c in ".?!")
        if cut >= 20:
            cleaned = cleaned[: cut + 1].strip()
            failures.append("trimmed_incomplete_sentence")
        elif not cleaned.endswith(("...", ",")):
            # Em-dash and en-dash belong here beside the ASCII hyphen: the
            # first-chunk splitter can cut a clause at one, and without them the
            # trailing dash survives and gets a period bolted onto it, so TTS
            # reads "You're all caught up --." with a dangling beat.
            cleaned = cleaned.rstrip(" ,;:-—–") + "."
    ok = True
    hard = {
        "control_token",
        "role_leak",
        "persona_drift",
        "unsafe_action_claim",
        "empty",
        "malformed",
    }
    if hard.intersection(failures):
        ok = False
    return {
        "ok": ok,
        "failures": failures,
        "chars": len(cleaned),
        "text": cleaned if ok else "",
        "speak": cleaned if ok else _FALLBACK,
        "fallback": not ok,
        "generation_id": generation_id,
    }


# Tokens whose trailing "." is not a sentence end. Case-insensitive match on
# the word immediately before the period (internal periods kept, so "e.g"
# and "i.e" match as written). A lone capital letter (an initial, as in
# "J. R. Smith") is handled separately in _is_abbreviation_before_period.
_ABBREVIATIONS = frozenset(
    {
        "dr", "mr", "mrs", "ms", "st", "jr", "sr", "prof",
        "ave", "blvd", "rd", "ct",
        "e.g", "i.e", "etc", "vs", "approx", "no",
    }
)


def _preceding_token(buf: str, end: int) -> str:
    """Run of non-whitespace characters ending just before index ``end``."""
    start = end
    while start > 0 and not buf[start - 1].isspace():
        start -= 1
    return buf[start:end]


def _is_abbreviation_before_period(buf: str, period_index: int) -> bool:
    token = _preceding_token(buf, period_index)
    if not token:
        return False
    if token.lower() in _ABBREVIATIONS:
        return True
    return len(token) == 1 and token.isalpha() and token.isupper()


def first_complete_clause(
    buffer: str, *, min_chars: int = 24, extra_boundaries: str = ""
) -> tuple[str | None, str]:
    """Split first sentence-ending clause; return (clause_or_None, remainder).

    ``extra_boundaries`` adds characters as valid split points alongside the
    default ".?!" (e.g. ",;:—–" for a shorter first spoken unit). Empty by
    default, so every existing caller keeps today's sentence-only behaviour.

    A boundary character only counts if it ends the buffer or is followed by
    whitespace. Without this, "3:30", "82.5 percent", and "16:9" get mistaken
    for clause ends -- a ":"/"." glued to the next digit is mid-value, not a
    sentence boundary, for both the default ".?!" set and extra_boundaries.

    Two further carve-outs apply to "." specifically: a period preceded by a
    known abbreviation ("Dr.", "St.", "e.g.", a lone initial like "J.") is
    not a sentence end; and a period at the very end of the buffer preceded
    by a digit ("...is 3.") is treated as mid-decimal, not yet resolved --
    return (None, buffer) so the caller waits for more streamed input rather
    than guessing that "3." is the whole number.
    """
    buf = buffer or ""
    boundaries = ".?!" + extra_boundaries
    n = len(buf)
    for i, ch in enumerate(buf):
        if ch not in boundaries or i + 1 < min_chars:
            continue
        at_end = i + 1 >= n
        if not at_end and not buf[i + 1].isspace():
            continue
        if ch == ".":
            if _is_abbreviation_before_period(buf, i):
                continue
            if at_end and i > 0 and buf[i - 1].isdigit():
                continue
        clause = buf[: i + 1].strip()
        rest = buf[i + 1 :]
        if clause:
            return clause, rest
    return None, buf
