"""Turn policy: empty/thin/unmatched never reach unconstrained LLM generate."""
from __future__ import annotations

from dataclasses import dataclass

from nova_hailo.edge_harness.compact_codec import encode_call, encode_routed
from nova_hailo.edge_harness.router import (
    CLARIFY_SPEAK,
    MISSED_SPEAK,
    is_search_again,
    looks_like_frustration,
    looks_like_joke,
    looks_like_place_search,
)
from nova_hailo.edge_harness.task_state import TaskState
from nova_hailo.web.fail_closed import FAIL_CLOSED_SPEAK

# Too-thin: ASR junk / hesitation only — not a real turn.
_THIN_RE_MIN_ALPHA = 3


@dataclass(frozen=True)
class TurnDecision:
    path: str  # empty | thin | clarify | search_again | tool | chat
    speak: str | None
    invoke_llm: bool
    reuse_last_tool: bool = False


def is_thin_utterance(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    letters = sum(1 for c in t if c.isalpha())
    return letters < _THIN_RE_MIN_ALPHA


def decide_turn(
    user_text: str,
    state: TaskState,
    *,
    routed_intent: str | None,
) -> TurnDecision:
    """Decide host path. invoke_llm is True only for explicit chat-family routes."""
    t = (user_text or "").strip()
    if not t:
        return TurnDecision("empty", FAIL_CLOSED_SPEAK, False)
    if is_thin_utterance(t):
        return TurnDecision("thin", FAIL_CLOSED_SPEAK, False)
    if is_search_again(t) and (state.last_tool or state.last_query):
        return TurnDecision("search_again", None, False, reuse_last_tool=True)
    if routed_intent is None:
        # Router miss: prefer search for POI, then bounded chat — never loop
        # the same clarify line forever (live: Los Gatos coffee / "can you do").
        if looks_like_place_search(t):
            return TurnDecision("force_search", None, False)
        if looks_like_joke(t) or state.clarify_count >= 1:
            return TurnDecision("chat", None, True)
        if looks_like_frustration(t):
            return TurnDecision("clarify", MISSED_SPEAK, False)
        return TurnDecision("clarify", CLARIFY_SPEAK, False)
    if routed_intent in {
        "identity",
        "capability",
        "smalltalk",
        "capability_unavailable",
    }:
        return TurnDecision("chat", None, False)  # canned via router/broker
    return TurnDecision("tool", None, False)


def allows_free_chat_generate(decision: TurnDecision) -> bool:
    return bool(decision.invoke_llm)


def gate_after_router(
    user_text: str,
    state: TaskState,
    tool_out: dict | None,
) -> TurnDecision | None:
    """Host gate used by the pipeline after OemToolGateway.route_and_execute.

    None → caller already has a routed tool/canned result to speak.
    Otherwise → speak ``decision.speak``; never call unconstrained generate.
    """
    if tool_out is not None:
        return None
    return decide_turn(user_text, state, routed_intent=None)


def host_turn(gateway, user_text: str) -> dict:
    """One host-owned turn: gateway first, then gate. Never selects free chat.

    ``gateway`` is an OemToolGateway (or a duck-type with route_and_execute
    + task_state + last_clear_llm).
    """
    tool_out = gateway.route_and_execute(user_text)
    state = getattr(gateway, "task_state", None) or TaskState()
    decision = gate_after_router(user_text, state, tool_out)
    if tool_out is not None:
        name = str(tool_out.get("name") or "")
        return {
            "path": "tool",
            "speak": str(tool_out.get("speak") or ""),
            "invoke_llm": False,
            "tool_out": tool_out,
            "clear_llm": bool(getattr(gateway, "last_clear_llm", False)),
            "codec": encode_routed(name, user_text),
        }
    assert decision is not None
    if decision.path == "force_search" and hasattr(gateway, "execute"):
        forced = gateway.execute("web_search", query=user_text)
        return {
            "path": "tool",
            "speak": str(forced.get("speak") or ""),
            "invoke_llm": False,
            "tool_out": forced,
            "clear_llm": False,
            "codec": encode_call("t0", query=user_text),
        }
    codec = None
    if decision.path == "clarify" and decision.speak:
        codec = encode_call("t5", message=decision.speak)
    elif decision.path == "chat" and decision.speak:
        codec = encode_call("t5", message=decision.speak)
    return {
        "path": decision.path,
        "speak": decision.speak,
        "invoke_llm": allows_free_chat_generate(decision),
        "tool_out": None,
        "clear_llm": False,
        "reuse_last_tool": decision.reuse_last_tool,
        "codec": codec,
    }
