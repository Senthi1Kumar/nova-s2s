"""LFM2.5-230M tool-agent loop: pick tools via chat-completions, execute, return results.

Used when ``NOVA_TOOL_ROUTE_MODE=model``. The speak LLM (350M) never sees the full
toolbox — only the speak payloads from this agent.

Callers: ``nova.server.tool_service`` ``POST /tools/agent``; s2s speak path via
``NOVA_TOOLS_AGENT_URL``. Plan: LFM230M tool agent + 350M articulator.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from nova.server.routing import (
    _is_confirm_only,
    _is_low_signal,
    _is_stop_only,
    _normalize_stt_query,
    route_mode,
)
from nova.server.session_state import (
    email_mode_from_query,
    get_session,
    ground_send_email_args,
    remember_tool_result,
    resolve_drive_ordinal,
    unsupported_capability,
)
from nova.tools.base import NovaTool

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROUTER_PROMPT_PATH = _REPO_ROOT / "prompts" / "tool_router.md"
_NO_TOOL_RE = re.compile(r"^\s*NO_TOOL\s*$", re.I)
_NEWS_WORDS = frozenset({"news", "headline", "headlines", "breaking"})
_STOCK_WORDS = frozenset(
    {"stock", "stocks", "share", "shares", "ticker", "nasdaq", "nyse", "equity"}
)
_WEATHER_WORDS = frozenset(
    {
        "weather",
        "forecast",
        "outside",
        "outdoor",
        "outdoors",
        "rain",
        "raining",
        "humid",
        "humidity",
        "temperature",
    }
)
# Place names often spoken with local news (keep tiny; STT aliases).
_PLACE_HINTS = {
    "bangalore": "Bangalore",
    "bengaluru": "Bangalore",
    "chennai": "Chennai",
    "mumbai": "Mumbai",
    "delhi": "Delhi",
    "hyderabad": "Hyderabad",
    "pune": "Pune",
}


def _query_tokens(query: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (query or "").lower()))



_CALENDAR_WORDS = frozenset({
    "calendar", "calendars", "schedule", "schedules", "appointment",
    "appointments", "meeting", "meetings", "agenda",
})
_REMINDER_ONLY = frozenset({"reminder", "reminders", "remind"})


def _calendar_day_args(query: str) -> dict[str, str]:
    q = (query or "").lower()
    if "day after tomorrow" in q or "day after" in q:
        return {"day": "day_after_tomorrow"}
    if "tomorrow" in q:
        return {"day": "tomorrow"}
    if "today" in q:
        return {"day": "today"}
    return {"day": "week"}


def _forced_calendar(query: str) -> dict[str, str] | None:
    """Skip 230M when the utterance is clearly a calendar check (not reminders).

    Live failure: 230M picked list_reminders for "check my calendar" and STT
    mangling like "Jackman can refer tomorrow" (≈ calendar for tomorrow).
    """
    q = (query or "").lower()
    tokens = _query_tokens(query)
    calish = bool(re.search(r"\bcalend\w*|\bcandler\w*|\bcanlon\w*", q))
    calish = calish or bool(tokens & _CALENDAR_WORDS)
    # STT: "can refer" / "refer tomorrow" near check ≈ calendar
    if not calish and ("tomorrow" in tokens or "today" in tokens):
        if "refer" in tokens or ("check" in tokens and "email" not in tokens and "mail" not in tokens):
            # Only if not clearly reminders
            if not (tokens & _REMINDER_ONLY):
                calish = "refer" in tokens or bool(
                    re.search(r"\b(check|show|what.?s|whats)\b.*\b(tomorrow|today)\b", q)
                )
    if not calish:
        return None
    if (tokens & _REMINDER_ONLY) and not (tokens & _CALENDAR_WORDS) and not re.search(
        r"calend", q
    ):
        return None
    return _calendar_day_args(query)



def _forced_email(query: str) -> dict[str, str] | None:
    """Force check_email for inbox asks, including STT 'check my E'."""
    q = _normalize_stt_query(query).lower()
    tokens = _query_tokens(q)
    anchors = {"email", "emails", "mail", "mails", "inbox", "gmail"}
    if not (tokens & anchors):
        return None
    # Compose/reply stays on send_email (router + session grounding).
    if tokens & {"reply", "draft", "respond", "send", "compose", "write"}:
        return None
    if re.search(r"\bwrite\s+back\b", q):
        return None
    if tokens & _WEATHER_WORDS:
        return None
    return {"mode": email_mode_from_query(q)}


def _forced_web_search(query: str) -> dict[str, Any] | None:
    """Lexical override when 230M chronically picks get_weather for news/stock.

    Returns web_search args, or None to let the router LLM decide.
    """
    tokens = _query_tokens(query)
    if not tokens or bool(tokens & _WEATHER_WORDS):
        return None
    tickers = {"amazon", "apple", "google", "tesla", "nvidia", "meta", "microsoft"}
    wants_news = bool(tokens & _NEWS_WORDS)
    wants_stock = bool(tokens & _STOCK_WORDS) or (
        "price" in tokens and bool(tokens & tickers)
    )
    wants_search = "search" in tokens and bool(
        tokens - {"search", "for", "the", "a", "me", "can", "you", "please", "hey"}
    )
    if not (wants_news or wants_stock or wants_search):
        return None

    args: dict[str, Any] = {"query": query.strip()}
    if wants_news:
        for tok, place in _PLACE_HINTS.items():
            if tok in tokens:
                args["place"] = place
                break
    return args


def _correct_calls(
    query: str,
    calls: list[dict[str, Any]],
    *,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Rewrite clear mis-routes and fill email/Drive follow-up args."""
    tokens = _query_tokens(query)
    scratch = get_session(session_id)
    out: list[dict[str, Any]] = []
    force = None if bool(tokens & _WEATHER_WORDS) else _forced_web_search(query)
    cal = _forced_calendar(query)
    forced_email = _forced_email(query)
    for call in calls:
        name = call.get("name") or ""
        args = dict(call.get("args") or {})
        if cal is not None and name in {"list_reminders", "query_calendar", "set_reminder"}:
            out.append(
                {
                    "id": call.get("id") or "",
                    "name": "check_calendar",
                    "args": dict(cal),
                }
            )
            continue
        if forced_email is not None and name in {"get_weather", "web_search", "list_reminders"}:
            out.append(
                {
                    "id": call.get("id") or "",
                    "name": "check_email",
                    "args": dict(forced_email),
                }
            )
            continue
        if force is not None and name == "get_weather":
            out.append(
                {
                    "id": call.get("id") or "",
                    "name": "web_search",
                    "args": dict(force),
                }
            )
            continue
        if force is not None and name == "web_search":
            for junk in ("temp_c", "condition", "country", "humidity"):
                args.pop(junk, None)
            if not (args.get("query") or "").strip():
                args = dict(force)
        if name == "check_email":
            # Live bug: 230M omitted mode=latest/summarize → unread default.
            mode = (args.get("mode") or "").strip().lower()
            if mode not in {"unread", "latest", "summarize"}:
                args["mode"] = email_mode_from_query(query)
            else:
                # Still upgrade unread→summarize/latest when the utterance is clear.
                inferred = email_mode_from_query(query)
                if inferred != "unread":
                    args["mode"] = inferred
        if name == "send_email":
            args = ground_send_email_args(query, args, scratch)
        out.append({**call, "args": args})
    return out


def _router_system_prompt() -> str:
    if _ROUTER_PROMPT_PATH.is_file():
        return _ROUTER_PROMPT_PATH.read_text().strip()
    return (
        "You are a tool agent. Call tools when needed, otherwise reply exactly NO_TOOL."
    )


def _openai_tools(registry: dict[str, NovaTool]) -> list[dict[str, Any]]:
    """Chat-completions tool schema shape (nested ``function``)."""
    out: list[dict[str, Any]] = []
    for tool in registry.values():
        ft = tool.to_function_tool()
        out.append(
            {
                "type": "function",
                "function": {
                    "name": ft["name"],
                    "description": ft.get("description") or "",
                    "parameters": ft.get("parameters")
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def _parse_tool_calls(message: Any) -> list[dict[str, Any]]:
    """Normalize OpenAI-style tool_calls from a chat completion message."""
    raw = getattr(message, "tool_calls", None) or []
    if not raw and isinstance(message, dict):
        raw = message.get("tool_calls") or []
    calls: list[dict[str, Any]] = []
    for tc in raw:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name")
            args_s = fn.get("arguments") or tc.get("arguments") or "{}"
            call_id = tc.get("id") or ""
        else:
            fn = getattr(tc, "function", None)
            name = (
                getattr(fn, "name", None)
                if fn is not None
                else getattr(tc, "name", None)
            )
            args_s = (
                getattr(fn, "arguments", None)
                if fn is not None
                else getattr(tc, "arguments", None)
            ) or "{}"
            call_id = getattr(tc, "id", "") or ""
        if not name:
            continue
        try:
            args = json.loads(args_s) if isinstance(args_s, str) else (args_s or {})
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append({"id": call_id, "name": str(name), "args": args})
    return calls


def _message_text(message: Any) -> str:
    if message is None:
        return ""
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", None) or "")


def _router_base_url() -> str:
    return (os.environ.get("NOVA_ROUTER_LLM_URL") or "").strip().rstrip("/")


def _call_router_llm(
    registry: dict[str, NovaTool], query: str, *, timeout_s: float = 12.0
) -> tuple[list[dict[str, Any]], str]:
    """Ask the 230M router for tool calls. Returns (calls, assistant_text)."""
    base = _router_base_url()
    if not base:
        raise RuntimeError("NOVA_ROUTER_LLM_URL is not set")
    payload = {
        "model": "tool-router",
        "messages": [
            {"role": "system", "content": _router_system_prompt()},
            {"role": "user", "content": query},
        ],
        "tools": _openai_tools(registry),
        "tool_choice": "auto",
        "max_tokens": 256,
        "temperature": 0.1,
    }
    with httpx.Client(timeout=timeout_s) as client:
        resp = client.post(f"{base}/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        return [], ""
    message = choices[0].get("message") or {}
    return _parse_tool_calls(message), _message_text(message)


def _execute_one(
    registry: dict[str, NovaTool],
    name: str,
    args: dict[str, Any],
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    tool = registry.get(name)
    if tool is None:
        return {"error": f"unknown tool: {name}"}
    call_args = dict(args)
    if name == "send_payment" and session_id and "session_id" not in call_args:
        call_args["session_id"] = session_id
    try:
        result = tool.execute(**call_args)
    except TypeError as exc:
        return {"error": f"invalid args for tool '{name}': {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"tool '{name}' failed: {exc}"}
    if not isinstance(result, dict):
        return {"result": result}
    remember_tool_result(session_id, name, result)
    return result


def _speak_blob(results: list[dict[str, Any]]) -> str:
    """Join ready-to-speak tool text only — never prefix tool names (350M echoes them)."""
    parts: list[str] = []
    for item in results:
        result = item.get("result") or {}
        if isinstance(result, dict):
            speak = (result.get("speak") or "").strip()
            if speak:
                parts.append(speak)
                continue
            parts.append(json.dumps(result, ensure_ascii=False)[:800])
        else:
            parts.append(str(result)[:800])
    return "\n".join(parts)


def run_tool_agent(
    registry: dict[str, NovaTool],
    query: str,
    *,
    execute: bool = True,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Run low-signal guards + optional 230M tool call + execute.

    Return shape consumed by s2s speak path and ``/tools/agent``.
    """
    q = _normalize_stt_query((query or "").strip())
    # Articulator contract: speak turn must see zero tools (LFM markup leak).
    articulator_tools: list[dict[str, Any]] = []

    if _is_stop_only(q):
        return {
            "needs_tools": False,
            "skipped": "stop",
            "tool_calls": [],
            "results": [],
            "speak_payload": "Okay.",
            "speak_instructions": (
                "Say only: Okay. Do not mention weather, email, calendar, or travel."
            ),
            "tools": articulator_tools,
            "tool_choice": "none",
        }

    if not q or _is_low_signal(q) or _is_confirm_only(q):
        return {
            "needs_tools": False,
            "skipped": "low_signal",
            "tool_calls": [],
            "results": [],
            "speak_payload": "",
            "speak_instructions": "",
            "tools": articulator_tools,
            "tool_choice": "none",
        }

    if route_mode() != "model":
        return {
            "needs_tools": False,
            "skipped": "mode_not_model",
            "tool_calls": [],
            "results": [],
            "speak_payload": "",
            "speak_instructions": "",
            "tools": articulator_tools,
            "tool_choice": "none",
        }

    # Unsupported ops (e.g. Drive delete) — capability error, never fabricate.
    unsupported = unsupported_capability(q)
    if unsupported is not None:
        speak = unsupported.get("speak") or ""
        return {
            "needs_tools": True,
            "skipped": None,
            "tool_calls": [],
            "results": [{"name": "unsupported", "args": {}, "result": unsupported}],
            "speak_payload": speak,
            "speak_instructions": (
                "Tool results for this turn (speak these to the driver; do not invent "
                "numbers or call tools):\n"
                f"unsupported: {speak}"
            )
            if speak
            else "",
            "tools": articulator_tools,
            "tool_choice": "none",
        }

    # Ordinal Drive follow-up against last listing (no router round-trip).
    scratch = get_session(session_id)
    ordinal = resolve_drive_ordinal(q, scratch.last_drive_files)
    if ordinal is not None and any(
        w in q.lower() for w in ("file", "document", "doc", "drive")
    ):
        speak = ordinal.get("speak") or ""
        return {
            "needs_tools": True,
            "skipped": None,
            "tool_calls": [],
            "results": [{"name": "list_drive_files", "args": {"ref": "ordinal"}, "result": ordinal}],
            "speak_payload": speak,
            "speak_instructions": (
                "Tool results for this turn (speak these to the driver; do not invent "
                "numbers or call tools):\n"
                f"list_drive_files: {speak}"
            )
            if speak
            else "",
            "tools": articulator_tools,
            "tool_choice": "none",
        }

    # Email before 230M — STT "check my E" was routed to get_weather (live).
    forced_email = _forced_email(q)
    if forced_email is not None and "check_email" in registry:
        calls = [{"id": "forced_email", "name": "check_email", "args": forced_email}]
        text = ""
    # Calendar before 230M — it often picks list_reminders instead.
    elif (forced_cal := _forced_calendar(q)) is not None and "check_calendar" in registry:
        calls = [{"id": "forced_cal", "name": "check_calendar", "args": forced_cal}]
        text = ""
    # Clear news/stock intents: skip 230M (it often invents get_weather).
    elif (forced := _forced_web_search(q)) is not None and "web_search" in registry:
        calls = [{"id": "forced_web", "name": "web_search", "args": forced}]
        text = ""
    else:
        try:
            calls, text = _call_router_llm(registry, q)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool agent router call failed: %s", exc)
            return {
                "needs_tools": False,
                "skipped": "router_error",
                "error": str(exc),
                "tool_calls": [],
                "results": [],
                "speak_payload": "",
                "speak_instructions": "",
                "tools": articulator_tools,
                "tool_choice": "none",
            }

        if not calls or _NO_TOOL_RE.match(text or ""):
            return {
                "needs_tools": False,
                "skipped": "no_tool",
                "tool_calls": [],
                "results": [],
                "speak_payload": "",
                "speak_instructions": "",
                "tools": articulator_tools,
                "tool_choice": "none",
                "router_text": text,
            }
        calls = _correct_calls(q, calls, session_id=session_id)

    calls = _correct_calls(q, calls[:2], session_id=session_id)
    results: list[dict[str, Any]] = []
    if execute:
        for call in calls:
            results.append(
                {
                    "name": call["name"],
                    "args": call["args"],
                    "result": _execute_one(
                        registry, call["name"], call["args"], session_id=session_id
                    ),
                }
            )

    speak_payload = _speak_blob(results) if results else ""
    speak_instructions = ""
    if speak_payload:
        speak_instructions = (
            "Say the following to the driver verbatim. Do not say tool names. "
            "Do not invent numbers. Do not add filler:\n"
            + speak_payload
        )


    return {
        "needs_tools": True,
        "skipped": None,
        "tool_calls": calls,
        "results": results,
        "speak_payload": speak_payload,
        "speak_instructions": speak_instructions,
        # Speak/articulator path: always empty — nonempty schemas prime LFM markup.
        "tools": articulator_tools,
        "tool_choice": "none",
    }
