# nova_hailo/cabingym/slots.py
"""Deterministic slot parsers for vehicle commands (no model).

Callers: cabingym.grounding. A parser returns None when the text says nothing
it can parse; the caller then clarifies instead of guessing.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}


def parse_number(text: str) -> float | None:
    t = re.sub(r"[-,]", " ", (text or "").lower())
    half = 0.5 if re.search(r"\band\s+a\s+half\b", t) else 0.0
    m = re.search(r"(?<![\w.])(\d+(?:\.\d+)?)", t)
    if m:
        return float(m.group(1)) + half
    total, seen = 0, False
    for tok in t.split():
        if tok in _UNITS:
            total += _UNITS[tok]
            seen = True
        elif tok in _TENS:
            total += _TENS[tok]
            seen = True
        elif tok == "hundred":
            total = (total or 1) * 100
            seen = True
        elif seen:
            break
    return float(total) + half if seen else None


_WARM = re.compile(
    r"\b(warmer|hotter|heat\s+(it\s+)?up|turn\s+(the\s+)?(heat|temperature|temp)\s+up|too\s+cold|"
    r"(i'?m|it'?s|feeling)\s+(a\s+(bit|little)\s+)?(cold|freezing|chilly))\b", re.I)
_COOL = re.compile(
    r"\b(cooler|colder|cool\s+(it\s+)?down|turn\s+(the\s+)?(heat|temperature|temp)\s+down|"
    r"too\s+(hot|warm)|(i'?m|it'?s|feeling)\s+(a\s+(bit|little)\s+)?(hot|warm|boiling|sweating))\b",
    re.I)
_LOT = re.compile(r"\b(a\s+lot|much|way|really)\b", re.I)


def parse_temperature(text: str) -> dict[str, float] | None:
    n = parse_number(text)
    if n is not None:
        if re.search(r"\bfahrenheit\b", text, re.I) or 50 <= n <= 90:
            n = (n - 32) * 5 / 9
        if 10 <= n <= 40:
            return {"value": round(n * 2) / 2}
    step = 2.0 if _LOT.search(text) else 1.0
    if _WARM.search(text):
        return {"delta": step}
    if _COOL.search(text):
        return {"delta": -step}
    return None


_UP = re.compile(r"\b(louder|up|higher|increase|raise|brighter|more)\b", re.I)
_DOWN = re.compile(r"\b(quieter|softer|down|lower|decrease|reduce|dimmer|dim|less|too\s+loud|"
                   r"too\s+bright)\b", re.I)


def parse_percent(text: str) -> dict[str, int] | None:
    t = (text or "").lower()
    n = parse_number(t)
    if n is not None and 0 <= n <= 100:
        return {"value": int(round(n))}
    if re.search(r"\b(max(imum)?|full|all\s+the\s+way\s+up|highest)\b", t):
        return {"value": 100}
    if re.search(r"\b(mute|minimum|min|silent)\b", t):
        return {"value": 0}
    if re.search(r"\bhalf(way)?\b", t):
        return {"value": 50}
    if _DOWN.search(t):
        return {"delta": -10}
    if _UP.search(t):
        return {"delta": 10}
    return None


def parse_opening(text: str) -> dict[str, int] | None:
    t = (text or "").lower()
    m = re.search(r"(\d+)\s*(%|percent)", t)
    if m:
        return {"value": int(m.group(1))}
    if re.search(r"\btilt", t):
        return {"value": -100}
    if re.search(r"\bhalf(way)?\b", t):
        return {"value": 50}
    if re.search(r"\b(close|shut|roll\s+(it\s+|them\s+)?up|put\s+(it\s+|them\s+)?up|raise)\b", t) \
            and not re.search(r"\bopen\b", t):
        return {"value": 0}
    if re.search(r"\b(crack|vent|a\s+(bit|little|crack)|slightly)\b", t):
        return {"value": 10}
    if re.search(r"\b(open|roll\s+(it\s+|them\s+)?down|put\s+(it\s+|them\s+)?down|lower|"
                 r"all\s+the\s+way)\b", t):
        return {"value": 100}
    n = parse_number(t)
    if n is not None and 0 <= n <= 100:
        return {"value": int(n)}
    return None


def parse_shade(text: str) -> dict[str, int] | None:
    t = (text or "").lower()
    if re.search(r"\b(up|raise|close|deploy|extend|pull\s+up)\b", t):
        return {"value": 100}
    if re.search(r"\b(down|lower|open|retract)\b", t):
        return {"value": 0}
    return None


def parse_level(text: str, max_level: int, on_level: int) -> dict[str, int]:
    t = (text or "").lower()
    if re.search(r"\b(off|stop|disable)\b", t):
        return {"value": 0}
    if re.search(r"\b(max(imum)?|full|highest|all\s+the\s+way)\b", t):
        return {"value": max_level}
    n = parse_number(t)
    if n is not None and 0 <= n <= max_level and float(n).is_integer():
        return {"value": int(n)}
    if re.search(r"\bhigh\b", t):
        return {"value": 3 if max_level == 3 else 5}
    if re.search(r"\b(medium|mid|middle)\b", t):
        return {"value": 2 if max_level == 3 else 3}
    if re.search(r"\blow\b", t):
        return {"value": 1}
    if re.search(r"\b(up|higher|more|increase|stronger|faster)\b", t):
        return {"delta": 1}
    if re.search(r"\b(down|lower|less|decrease|weaker|slower)\b", t):
        return {"delta": -1}
    return {"value": on_level}


_OFF = re.compile(r"\b(off|disable|deactivate|stop|cancel|no\s+more)\b", re.I)
_ON = re.compile(r"\b(on|enable|activate|start)\b", re.I)


def parse_on_off(text: str) -> bool | None:
    if _OFF.search(text or ""):
        return False
    if _ON.search(text or ""):
        return True
    return None


def parse_lock(text: str) -> bool | None:
    if re.search(r"\bunlock", text or "", re.I):
        return False
    if re.search(r"\block", text or "", re.I):
        return True
    return None


def parse_fold(text: str) -> bool | None:
    if re.search(r"\b(unfold|extend|open)\b", text or "", re.I):
        return False
    if re.search(r"\b(fold|tuck|close)\b", text or "", re.I):
        return True
    return None


def parse_recirc(text: str) -> bool | None:
    if re.search(r"\b(fresh|outside)\s+air\b", text or "", re.I):
        return False
    return parse_on_off(text)


def parse_mute(text: str) -> bool | None:
    if re.search(r"\bunmute\b", text or "", re.I):
        return False
    if re.search(r"\bmute\b", text or "", re.I):
        return True
    return None


_ZONE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("all", re.compile(
        r"\b(everyone|everybody|both\s+sides|whole\s+car|all\s+(seats|zones|sides|windows|around)|"
        r"driver\s+and\s+(the\s+)?passenger|passenger\s+and\s+(the\s+)?driver|every\s+window|"
        r"the\s+windows)\b", re.I)),
    ("rear_left", re.compile(r"\b(rear|back)\s+left\b", re.I)),
    ("rear_right", re.compile(r"\b(rear|back)\s+right\b", re.I)),
    ("rear", re.compile(r"\b(rear|back)\b", re.I)),
    ("passenger", re.compile(r"\bpassenger\b", re.I)),
    ("front", re.compile(r"\bfront\b", re.I)),
    ("driver", re.compile(r"\b(driver'?s?|my\s+(side|seat|window)|for\s+me|i'?m|i\s+am)\b", re.I)),
)
_REAR_ZONES = frozenset({"rear", "rear_left", "rear_right"})


# A follow-up that does not name a new zone: a yes, a number, or "it" / warmer.
_CONTINUES = re.compile(
    r"\b(yes|yeah|yep|yup|ok|okay|sure|please|degrees?|it|that|same|warmer|cooler|colder|hotter)\b|\d",
    re.I,
)


def continues_request(text: str) -> bool:
    return bool(_CONTINUES.search(text or ""))


def parse_zone(
    text: str, allowed: Sequence[str], default: str | None = None
) -> tuple[str | None, str]:
    for zone, pat in _ZONE_PATTERNS:
        if not pat.search(text or ""):
            continue
        if zone in allowed:
            return zone, "parser"
        if zone in _REAR_ZONES:
            return None, "unsupported"
    if default is not None and default in allowed:
        return default, "default"
    return None, "missing"


def parse_enum(
    text: str,
    allowed: Sequence[str],
    synonyms: Mapping[str, Sequence[str]] | None = None,
    bool_map: Mapping[str, str] | None = None,
) -> str | None:
    t = (text or "").lower()
    for value in allowed:
        words = tuple((synonyms or {}).get(value, ())) + (value.replace("_", " "),)
        for word in words:
            if re.search(rf"\b{re.escape(word.lower())}\b", t):
                return value
    if bool_map:
        on = parse_on_off(t)
        if on is not None:
            return bool_map.get("true" if on else "false")
    return None


_MSG_MARKER = re.compile(r"\b(?:saying|that\s+says|to\s+say|that)\b[:,]?\s*(?P<body>.+)$", re.I)
_MSG_AFTER_NAME = re.compile(
    r"\b(?:text|message|tell)\s+(?P<name>[A-Za-z]+)\s*[:,]?\s+(?P<body>.+)$", re.I)


def extract_message(text: str) -> str | None:
    m = _MSG_MARKER.search(text or "") or _MSG_AFTER_NAME.search(text or "")
    if not m:
        return None
    body = m.group("body").strip().strip("\"'“”").rstrip(".!?").strip()
    return body or None


_PLAY = re.compile(
    r"\bplay\s+(?:me\s+)?(?:some\s+)?(?:the\s+)?(?P<q>.+?)"
    r"(?:\s+on\s+(?:spotify|the\s+radio|radio|bluetooth|usb))?\s*[.!?]*$", re.I)


def extract_media_query(text: str) -> str | None:
    m = _PLAY.search(text or "")
    if not m:
        return None
    q = m.group("q").strip()
    return q or None


def match_entity(name: str, candidates: Sequence[str]) -> str | None:
    n = (name or "").strip().casefold()
    if not n:
        return None
    for cand in candidates:
        if cand.casefold() == n:
            return cand
    prefixed = [c for c in candidates if c.casefold().startswith(n)]
    return prefixed[0] if len(prefixed) == 1 else None


def find_entity_in_text(text: str, candidates: Sequence[str]) -> str | None:
    t = (text or "").casefold()
    hits = [c for c in candidates if re.search(rf"\b{re.escape(c.casefold())}\b", t)]
    return max(hits, key=len) if hits else None


_VERBS = (
    r"(?:turn|set|make|open|close|lock|unlock|play|pause|resume|skip|call|dial|text|send|message|"
    r"navigate|take|drive|go|start|stop|put|raise|lower|increase|decrease|switch|read|answer|"
    r"reject|decline|end|hang|mute|unmute|find|show|fold|tilt|shut|enable|disable|activate|heat|"
    r"cool|warm|defrost|add|crank|roll|pop|cancel|what|how|where)"
)
_NOUNS = (
    r"(?:the\s+)?(?:volume|temperature|temp|fan|seats?|windows?|sunroof|lights?|headlights|"
    r"wipers|music|radio|ac|a/c|heat|mirrors|trunk|doors)"
)
_SPLIT = re.compile(
    rf"\s*(?:\band\s+then\b|\band\s+also\b|\bthen\b|\balso\b|\band\b|[,;])\s+"
    rf"(?=(?:please\s+)?(?:{_VERBS}|{_NOUNS})\b)", re.I)
_MSG_VERB = re.compile(r"\b(text|message|tell|send\s+(a\s+)?(text|message))\b", re.I)
_TRAILING_CONJ = re.compile(r"\s*\b(and|then|also)\s*$", re.I)


def split_clauses(text: str) -> list[str]:
    """Split a multi-intent utterance; a message body is never split."""
    t = (text or "").strip()
    if not t:
        return []
    m = _MSG_VERB.search(t)
    head, tail = (t[: m.start()], t[m.start():]) if m else (t, "")
    parts = [_TRAILING_CONJ.sub("", p).strip(" ,;") for p in _SPLIT.split(head)]
    parts = [p for p in parts if p]
    if tail.strip():
        parts.append(tail.strip())
    return parts
