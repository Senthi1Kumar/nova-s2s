"""Tool-result compressor (ROADMAP.md §2): never feed a raw MCP payload
(calendar/email/search hits) into LLM context -- emit a bounded typed block
with an explicit `instruction=` line instead.

Callers: nova_hailo.edge_harness.tool_broker (speak_summary(), for the
existing speak-now path); future LLM-context-injection callers (Workspace
tools returning structured results) use compress() directly.
"""
from __future__ import annotations

import re
from typing import Any


def _bullet_items(payload: dict[str, Any], max_items: int) -> list[str]:
    items = payload.get("events") or payload.get("results") or payload.get("files") or []
    if not isinstance(items, list):
        return []
    bits: list[str] = []
    for item in items[:max_items]:
        if isinstance(item, dict):
            text = str(item.get("title") or item.get("subject") or item.get("name") or "")[:80]
            if text:
                bits.append(text)
    return bits


def _voice_trim_list_speak(speak: str, *, max_items: int = 3) -> str:
    """Clamp long calendar/email enumerations so TTS does not recite 10 events.

    Live 2026-08-08: calendar speak listed 10 Legum Ai slots and overflowed
    attention. Prefer "A; B; C. And N more." when the tool already built a list.
    """
    text = (speak or "").strip()
    if not text:
        return text
    # Split on ";" or ". " list separators that calendar tools commonly use.
    parts = [p.strip(" .;") for p in re.split(r"\s*;\s*|\.\s+(?=[A-Z0-9])", text) if p.strip(" .;")]
    if len(parts) <= max_items + 1:
        return text
    head = parts[0]
    # First clause often "You have N upcoming events" — keep it, then first items.
    items = parts[1:] if re.search(r"\b(events?|emails?|messages?|files?)\b", head, re.I) else parts
    if len(items) <= max_items:
        return text
    shown = items[:max_items]
    more = len(items) - max_items
    if re.search(r"\b(events?|emails?|messages?|files?)\b", head, re.I) and head not in shown:
        body = "; ".join(shown)
        return f"{head.rstrip('.')}. {body}. And {more} more."
    body = "; ".join(shown)
    return f"{body}. And {more} more."


def speak_summary(payload: dict[str, Any], *, max_items: int = 3, fallback: str = "Done.") -> str:
    """Human-readable short spoken line from a raw MCP payload."""
    speak = str(payload.get("speak") or payload.get("summary") or "").strip()
    if speak:
        return _voice_trim_list_speak(speak, max_items=max_items)
    items = payload.get("events") or payload.get("results") or payload.get("files") or []
    if isinstance(items, list) and items:
        bits = _bullet_items(payload, max_items)
        more = len(items) - len(bits)
        if not bits:
            return "I found a few items."
        line = "; ".join(bits)
        if more > 0:
            return f"{line}. And {more} more."
        return line
    return fallback


def compress(
    name: str,
    payload: dict[str, Any],
    *,
    max_items: int = 3,
    max_chars: int = 250,
    instruction: str = "Speak only from the items above; do not invent details.",
) -> str:
    """Bounded typed block for LLM context, replacing a raw tool payload.

    ["[[tool_result name=check_calendar]]",
     "1. Team sync -- 10:00",
     "2. Dentist -- 15:30",
     "instruction=Speak only from the items above; do not invent details."]
    """
    bits = _bullet_items(payload, max_items)
    header = f"[[tool_result name={name}]]"
    footer = f"instruction={instruction}"
    body = "\n".join(f"{i}. {b}" for i, b in enumerate(bits, start=1)) if bits else "(no items)"
    # The instruction line is the whole point -- never truncate it away.
    # Trim the body instead, to whatever's left after header+footer+newlines.
    budget = max_chars - len(header) - len(footer) - 2
    if budget <= 0:
        body = ""
    elif len(body) > budget:
        body = body[: budget - 1] + "…"
    lines = [header] + ([body] if body else []) + [footer]
    return "\n".join(lines)
