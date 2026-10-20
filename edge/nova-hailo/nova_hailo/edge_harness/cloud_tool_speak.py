"""Groq/OpenRouter picked the tool; host chooses the spoken follow-up.

Clock / identity stay host-routed and are not in this module. Stocks stay in
the cloud toolbox (web_search). After ToolBroker returns, numeric results are
already a complete spoken line; an empty model rewrite must not drop that line.
"""
from __future__ import annotations

import re
from typing import Any

CLOUD_EMPTY_REWRITE_SPEAK = "I couldn't get that just now."

# Tool-message note after a numeric miss. The miss goes to the model's second
# round (so a follow-up like "Why is it?" gets answered), which reopens the
# door the numeric shortcut closed: a price recalled from training data.
NUMERIC_MISS_NOTE = (
    "The search results contain no reliable price or number for this. "
    "Do not state any price, number, or percentage. Tell the user you could "
    "not find a current figure, and answer what they actually asked."
)

# Tool-message note on a successful search. The system prompt marks tool
# results as untrusted web content (so the model never obeys instructions in
# them); without this, the model sometimes also refused to *use* them and
# answered from memory ("my info is a bit fuzzy on today's specifics").
SEARCH_RESULT_NOTE = (
    "These are current search results for the user's question. Answer from "
    "them; if they do not answer it, say that plainly."
)

# A spoken money amount or percentage: what a model invents after a miss.
_AMOUNT_RE = re.compile(
    r"[$€£₹]\s?\d"
    r"|\d[\d,]*(?:\.\d+)?\s?(?:%|percent\b|dollars?\b|usd\b|rupees?\b|inr\b|euros?\b)",
    re.I,
)


# A spoken claim that the car or the route changed. Only a vehicle tool that
# wrote this turn may say it; after a web search alone the model narrated
# "I've added a stop at Don Giovanni" and the cockpit showed nothing.
_CLAIM_RE = re.compile(
    r"\b(?:i(?:'ve|\s+have)|i\s+just|i'll|i\s+will|i'm)\s+(?:just\s+|now\s+|gone\s+ahead\s+and\s+)?"
    r"(?:added|add|set|turned|turn|switched|switch|opened|open|closed|close|started|start|changed|"
    r"adjusted|increased|decreased|lowered|raised|locked|unlocked|rerouted|reroute|routed|route|navigating|"
    r"updated|dimmed|brightened|cancell?ed|stopped)\b"
    r"|\b(?:added|adding)\s+(?:a\s+|the\s+|your\s+)?stop\b"
    r"|\b(?:added|adding)\s+[^.?!]{1,60}?\s+as\s+a\s+stop\b"
    r"|\bsetting\s+(?:the\s+|your\s+|a\s+)?route\b"
    r"|\b(?:now\s+)?(?:navigating|heading|routing)\s+(?:you\s+)?to\b"
    r"|\bis\s+now\s+(?:on|off|set|open|closed|at)\b",
    re.I,
)
CLAIM_WITHOUT_ACTION_SPEAK = "I haven't changed anything in the car yet."

# Asked to change something, as opposed to asked about something.
_ACTION_ASK_RE = re.compile(
    r"\b(add|set|turn|switch|open|close|start|change|adjust|increase|decrease|lower|raise|"
    r"lock|unlock|navigate|take\s+me|drive\s+(?:me\s+)?to|go\s+to|stop\s+at|reroute|"
    r"cancel|dim|brighten|play|call)\b",
    re.I,
)


def claims_vehicle_action(text: str) -> bool:
    """True when a spoken line says the car or the route was changed."""
    return bool(_CLAIM_RE.search(text or ""))


def asks_for_action(text: str) -> bool:
    """True when the driver asked for a change ("find X and add a stop")."""
    return bool(_ACTION_ASK_RE.search(text or ""))


def vehicle_acted(out: dict[str, Any] | None) -> bool:
    """True when a vehicle tool carried the request out (a change, or an honest
    no-op such as a fan already at that speed); its own line may then claim it."""
    if not isinstance(out, dict):
        return False
    result = out.get("result")
    return out.get("status") == "success" or bool(isinstance(result, dict) and result.get("diff"))


# Vehicle executor statuses that already are the line the driver should hear.
# "declined" is the status string; "decline" is the route-outcome kind.
_VEHICLE_FINAL_STATUS = frozenset({
    "success", "clarify", "decline", "declined", "confirm", "refused", "error",
})


def tool_result_is_final(out: dict[str, Any] | None) -> bool:
    """True when the host already composed the voice line.

    Numeric/stock quotes are final. A vehicle result is final too: the cloud
    model picked the tool, and a second round was rewriting "Where would you
    like to go?" into "heading there now". News and calendar stay open so the
    model can rewrite them.
    """
    if not isinstance(out, dict):
        return False
    speak = str(out.get("speak") or "").strip()
    if not speak:
        return False
    if out.get("name") == "vehicle" and str(out.get("status") or "") in _VEHICLE_FINAL_STATUS:
        return True
    result = out.get("result")
    if not isinstance(result, dict) or not result.get("numeric"):
        return False
    # A miss has nothing to quote. Treating it as final voiced the same
    # "couldn't find a number" line for every follow-up in the thread
    # ("Why is it?", "Can you search?") instead of answering them.
    return not result.get("numeric_miss")


def tool_log_detail(name: str, args: dict[str, Any] | None, out: dict[str, Any] | None) -> str:
    """One log fragment. Vehicle calls print action and slots; other tools print query.

    A grounded destination is a place dict. The log keeps the name, not the coordinates.
    """
    out = out or {}
    args = dict(args or {})
    vehicle = str(name).startswith("vehicle_") or out.get("name") == "vehicle"
    if not vehicle:
        query = str(args.get("query") or "")
        return f"query={query[:120]!r}"
    result = out.get("result")
    trace = (result.get("trace") if isinstance(result, dict) else {}) or {}
    calls = trace.get("calls") or []
    if calls and isinstance(calls[0], dict):
        action = str(calls[0].get("api") or "")
        slots = dict(calls[0].get("args") or {})
    else:
        slots = args
        action = str(slots.get("action") or trace.get("action") or "")
    parts: list[str] = []
    for key in ("destination", "zone", "value", "category", "delta", "on"):
        if key not in slots or slots[key] in ("", None):
            continue
        val = slots[key]
        if isinstance(val, dict):
            val = val.get("name", val)
        parts.append(f"{key}={str(val)[:80]!r}")
    extra = (" " + " ".join(parts)) if parts else ""
    return f"action={action!r}{extra} status={out.get('status')}"


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


def is_numeric_miss(out: dict[str, Any] | None) -> bool:
    """True when a price/number lookup ran and found nothing to quote."""
    if not isinstance(out, dict):
        return False
    result = out.get("result")
    return bool(isinstance(result, dict) and result.get("numeric") and result.get("numeric_miss"))


def tool_message_content(out: dict[str, Any]) -> dict[str, Any]:
    """Compact tool result for the model's next round (not the raw payload)."""
    content = {
        "ok": out.get("ok"),
        "status": out.get("status"),
        "speak": str(out.get("speak") or "")[:800],
    }
    result = out.get("result")
    if is_numeric_miss(out):
        content["note"] = NUMERIC_MISS_NOTE
    elif (
        out.get("ok")
        and isinstance(result, dict)
        and result.get("evidence")
        and not result.get("numeric")
    ):
        content["note"] = SEARCH_RESULT_NOTE
    return content


def states_amount(text: str) -> bool:
    """True when text speaks a money amount or a percentage."""
    return bool(_AMOUNT_RE.search(text or ""))


def guard_numeric_miss_text(text: str, *, forbid: bool, miss_speak: str) -> str:
    """After a numeric miss, never voice a number the search did not contain."""
    if forbid and states_amount(text):
        return miss_speak
    return text
