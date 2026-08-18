"""Octopus-v2-style compact action codec — no tool schema in the LLM prompt.

Logical tokens t0..t6 map to the OEM allowlist (token_map.json).
Parse is fail-closed: unknown token, extra keys, or missing required args
return None. The host never executes a raw model string.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_MAP_PATH = Path(__file__).with_name("token_map.json")
_CALL_RE = re.compile(
    r"^\s*(?P<tok>t[0-6])\s*\((?P<body>.*)\)\s*$",
    re.S,
)
_KV_RE = re.compile(
    r"(?P<k>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?:\"(?P<dq>(?:\\.|[^\"])*)\"|'(?P<sq>(?:\\.|[^'])*)'|(?P<bare>[^,)]+))"
)


@lru_cache(maxsize=1)
def load_token_map(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else _MAP_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "tokens" not in data:
        raise ValueError("token_map.json missing tokens")
    return data


def tool_for_token(token: str) -> str | None:
    spec = load_token_map()["tokens"].get(token)
    return None if spec is None else str(spec["tool"])


def token_for_tool(tool: str) -> str | None:
    for tok, spec in load_token_map()["tokens"].items():
        if spec.get("tool") == tool:
            return tok
    return None


def encode_call(token: str, **args: Any) -> str:
    """Emit Mercedes-style named args; omit None/empty optionals."""
    parts: list[str] = []
    for key, val in args.items():
        if val is None:
            continue
        s = str(val)
        if s == "" and key != "query":
            continue
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'{key}="{escaped}"')
    return f"{token}({', '.join(parts)})"


def parse_call(text: str) -> dict[str, Any] | None:
    """Parse one call. None = invalid (do not execute)."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:\w+)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    m = _CALL_RE.match(raw)
    if not m:
        return None
    token = m.group("tok")
    spec = load_token_map()["tokens"].get(token)
    if spec is None:
        return None
    args: dict[str, str] = {}
    body = (m.group("body") or "").strip()
    if body:
        for km in _KV_RE.finditer(body):
            val = km.group("dq")
            if val is None:
                val = km.group("sq")
            if val is None:
                val = (km.group("bare") or "").strip()
            else:
                val = val.replace('\\"', '"').replace("\\\\", "\\")
            args[km.group("k")] = val
    allowed = set(spec.get("required") or []) | set(spec.get("optional") or [])
    if any(k not in allowed for k in args):
        return None
    for req in spec.get("required") or []:
        if not str(args.get(req) or "").strip():
            return None
    return {"token": token, "tool": spec["tool"], "args": args}


def encode_routed(intent: str, query: str, slots: dict[str, Any] | None = None) -> str | None:
    """Host-side: turn a routed intent into a codec line (logs / later LoRA targets)."""
    slots = slots or {}
    if intent in {"identity", "capability", "smalltalk", "capability_unavailable"}:
        speak = str(slots.get("speak") or query)
        return encode_call("t5", message=speak)
    tok = token_for_tool(intent)
    if tok is None:
        return None
    if intent == "web_search" or intent == "deep_research":
        return encode_call(tok, query=query)
    if intent == "check_calendar":
        return encode_call(tok, day=slots.get("day") or None)
    if intent in {"check_email", "list_drive_files"}:
        return encode_call(tok, query=query or None)
    return encode_call(tok)
