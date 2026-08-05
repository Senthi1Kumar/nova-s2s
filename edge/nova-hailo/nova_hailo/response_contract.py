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
    if cleaned and cleaned[-1] not in ".?!":
        cut = max(cleaned.rfind(c) for c in ".?!")
        if cut >= 20:
            cleaned = cleaned[: cut + 1].strip()
            failures.append("trimmed_incomplete_sentence")
        elif not cleaned.endswith(("...", ",")):
            cleaned = cleaned.rstrip(" ,;:-") + "."
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


def first_complete_clause(buffer: str, *, min_chars: int = 24) -> tuple[str | None, str]:
    """Split first sentence-ending clause; return (clause_or_None, remainder)."""
    buf = buffer or ""
    for i, ch in enumerate(buf):
        if ch in ".?!" and i + 1 >= min_chars:
            clause = buf[: i + 1].strip()
            rest = buf[i + 1 :]
            if clause:
                return clause, rest
    return None, buf
