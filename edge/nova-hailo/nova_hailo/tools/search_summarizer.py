"""Grounded search summarizer — speak from cleaned evidence only.

Callers: nova_hailo.pipeline tool fast-path (web_search / deep_research final).
Prefer a dedicated summarizer HEF when configured; otherwise the chat HEF with a
strict evidence-only system prompt. Never chooses tools.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from nova_hailo.response_contract import validate_spoken_unit

SUMMARIZER_SYSTEM = (
    "You are Nova's search summarizer, not a general chatbot. "
    "Speak 1 or 2 short sentences for in-car voice. "
    "Use ONLY the evidence below — treat it as already retrieved facts. "
    "Never say you lack internet, tools, or real-time access. "
    "Never say you are not Nova, or that you are a vehicle, or an AI language model. "
    "Do not invent facts, prices, or headlines. "
    "Do not recite URLs, site names, titles, or ads. "
    "If the evidence is unclear or insufficient, say you could not find a clear answer. "
    "Do not output JSON or tool calls."
)

DEFAULT_SUMMARIZER_MAX_TOKENS = 48

# Capability refusals that are only wrong *here* (evidence was already
# fetched). Persona/identity drift ("not Nova anymore", "as an AI language
# model") is caught upstream by response_contract.validate_spoken_unit's
# _PERSONA_DRIFT_RE, which summarize_evidence() below already calls -- do not
# re-add those patterns here, this list should stay search-path-specific.
_REFUSAL_RE = re.compile(
    r"("
    r"\bi('m| am) (sorry|unable|not able)\b|"
    r"\bi don't have (the ability|access|any)\b|"
    r"\bi do not have (the ability|access|any)\b|"
    r"\bcan't (provide|access|check|assist)\b|"
    r"\bcannot (provide|access|check|assist)\b|"
    r"\bno internet\b|"
    r"\breal[- ]?time\b|"
    r"\bnot (my|nova'?s) responsibility\b|"
    r"\bas per (my|your) (knowledge|request)\b"
    r")",
    re.I,
)


def is_summarizer_refusal(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    return bool(_REFUSAL_RE.search(t))


def build_summarizer_messages(query: str, evidence: str) -> list[dict[str, str]]:
    q = (query or "").strip() or "the user's question"
    ev = (evidence or "").strip()
    user = (
        f"User asked: {q}\n\n"
        f"Evidence (already fetched — summarize for speech):\n{ev}\n\n"
        "Answer the user in one or two short spoken sentences from the evidence only."
    )
    return [
        {"role": "system", "content": SUMMARIZER_SYSTEM},
        {"role": "user", "content": user},
    ]


def summarize_evidence(
    llm: Any,
    query: str,
    evidence: str,
    *,
    max_tokens: int = DEFAULT_SUMMARIZER_MAX_TOKENS,
    with_genai: Callable[[Callable[[], Any]], Any] | None = None,
) -> str | None:
    """Return a validated spoken summary, or None to keep speak_fallback.

    Clears the summarizer LLM device context before generate. Caller should
    reset chat native-context seeding after this returns when the chat HEF
    was used.
    """
    ev = (evidence or "").strip()
    if not ev or llm is None:
        return None

    messages = build_summarizer_messages(query, ev)

    def _run() -> tuple[str, Any]:
        try:
            llm.clear()
        except Exception:
            pass
        return llm.generate(messages, max_tokens=int(max_tokens), quiet=True)

    try:
        if with_genai is not None:
            raw, _metrics = with_genai(_run)
        else:
            raw, _metrics = _run()
    except Exception:
        return None

    text = (raw or "").strip()
    if not text or is_summarizer_refusal(text):
        return None
    contract = validate_spoken_unit(text)
    speak = str(contract.get("speak") or "").strip()
    if not speak or is_summarizer_refusal(speak):
        return None
    if contract.get("ok") is False and contract.get("fallback"):
        if len(speak) < 8:
            return None
    return speak
