"""Groq/OpenRouter picked the tool; host chooses the spoken follow-up.

Clock / identity stay host-routed and are not in this module. Stocks stay in
the cloud toolbox (web_search). After ToolBroker returns, numeric results are
already a complete spoken line; an empty model rewrite must not drop that line.
"""
from __future__ import annotations

from typing import Any

CLOUD_EMPTY_REWRITE_SPEAK = "I couldn't get that just now."


def tool_result_is_final(out: dict[str, Any] | None) -> bool:
    """True when ToolBroker already composed the voice line (numeric/stock).

    ``answer_from_hits`` sets ``result.numeric`` and ``needs_summary`` False
    for stock/crypto quotes. News and calendar stay False so Groq can rewrite.
    """
    if not isinstance(out, dict):
        return False
    speak = str(out.get("speak") or "").strip()
    if not speak:
        return False
    result = out.get("result")
    return bool(isinstance(result, dict) and result.get("numeric"))


def resolve_cloud_followup_speak(
    *,
    llm_rewrite: str = "",
    tool_speak: str = "",
    tool_is_final: bool = False,
) -> str:
    """Pick the line to voice after a cloud tool round.

    Groq still chose the tool. Host then:
    - speaks the tool line when it is already final (numeric/stock)
    - uses Groq's rewrite when that rewrite has content (news, calendar)
    - falls back to the tool line when Groq returns empty content
    - uses CLOUD_EMPTY_REWRITE_SPEAK only when both are empty
    """
    tool = (tool_speak or "").strip()
    rewrite = (llm_rewrite or "").strip()
    if tool_is_final and tool:
        return tool
    if rewrite:
        return rewrite
    if tool:
        return tool
    return CLOUD_EMPTY_REWRITE_SPEAK
