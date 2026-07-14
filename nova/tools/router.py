"""Lexical tool router: weighted token-overlap scoring, zero latency, no model.

Given the driver's last utterance, scores each registered tool by counting
overlapping tokens between the query and the tool's name/parameters/description
(weighted name x3, params x2, description x1) and returns the top-k highest
scoring tools' function-tool schemas. Dependency-free, deterministic, no
embeddings or router model -- only escalate to a router model if eval shows
this recall failing.
"""
from __future__ import annotations

import re
from typing import Any

from nova.tools.base import NovaTool

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_NAME_WEIGHT = 3
_PARAM_WEIGHT = 2
_DESC_WEIGHT = 1

# Common function words carry no discriminating signal and can collide with
# short property names (e.g. send_email's "to" parameter vs. the preposition
# "to" in "pay fifty rupees to chai point") -- filter them from both the
# query and the index.
_STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "at", "for", "and", "or", "is",
    "it", "my", "your", "please", "can", "you", "i", "me", "with", "from",
}

# Small, demo-scoped synonym map: surface words the driver actually says,
# mapped to a canonical token that appears in the target tool's indexed
# name/params/description. NOT a general thesaurus -- only add entries that
# Prefer news/stock intents to web_search over weather misroutes.
_SYNONYMS: dict[str, str] = {
    "heat": "hvac",
    "heater": "hvac",
    "warm": "hvac",
    "cold": "hvac",
    "cool": "hvac",
    "ac": "hvac",
    "aircon": "hvac",
    "climate": "hvac",
    # "roll" (as in "roll the window up/down") is windows-specific idiom in
    # this domain, unlike the generic "up"/"down" alone -- those falsely
    # boosted set_windows for unrelated queries like "turn up the volume"
    # (set_hvac's own description mentions "windows" as an HVAC side effect,
    # and "driver" is a valid HVAC zone enum value, so ambiguous queries
    # genuinely compete; "roll" alone breaks ties toward windows correctly).
    "roll": "windows",
    # Cabin/vehicle status reads — "check my cabin temp" must not lose to
    # check_email solely because both share the verb "check".
    "cabin": "vehicle",
    "fuel": "vehicle",
    "range": "vehicle",
    "battery": "vehicle",
    # Zone/cabin status reads — do NOT map bare "tell"→status (that stole
    # "tell me the stock price" / "tell me any reminders" away from web_search
    # and list_reminders in live logs).
    "zones": "status",
    "zone": "status",
    # Outdoor / forecast language → get_weather (indexed "weather"); cabin
    # "temperature" alone still competes with set_hvac via its params.
    "forecast": "weather",
    "outside": "weather",
    "outdoor": "weather",
    "outdoors": "weather",
    "bangalore": "weather",
    "bengaluru": "weather",
    # Inbox / mail → check_email (name tokens include "email").
    "inbox": "email",
    "mails": "email",
    "gmail": "email",
    # Meetings → calendar tools (create/delete/check).
    "meeting": "calendar",
    "meetings": "calendar",
    # Reminders (list_reminders / set_reminder share "reminder" stem).
    "reminder": "reminders",
    "remind": "reminder",
    "reminders": "reminders",
    # Web / market / news facts → web_search (name token "search").
    "stock": "search",
    "stocks": "search",
    "ticker": "search",
    "share": "search",
    "shares": "search",
    "price": "search",
    "prices": "search",
    "google": "search",
    "lookup": "search",
    "news": "search",
    "headline": "search",
    "headlines": "search",
    "amazon": "search",
    "nvidia": "search",
    "spotify": "music",
    "jazz": "music",
    "song": "music",
    "playlist": "music",
    # Google Drive files.
    "drive": "files",
    "docs": "files",
    "document": "files",
    "documents": "files",
    "folder": "files",
}


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower())) - _STOPWORDS


def _expand_synonyms(tokens: set[str]) -> set[str]:
    """Union in each token's synonym-mapped canonical token, if any."""
    expanded = set(tokens)
    for tok in tokens:
        canonical = _SYNONYMS.get(tok)
        if canonical:
            expanded.add(canonical)
    return expanded


def _substring_match_count(query_tokens: set[str], field_tokens: set[str]) -> int:
    """Count query tokens (len >= 4) that are a prefix/suffix of some
    indexed token they don't already exactly match. Each query token
    credited at most once per field."""
    count = 0
    for qt in query_tokens:
        if len(qt) < 4:
            continue
        for ft in field_tokens:
            if qt == ft or len(ft) < 4:
                continue
            if ft.startswith(qt) or ft.endswith(qt) or qt.startswith(ft) or qt.endswith(ft):
                count += 1
                break
    return count


def _param_tokens(parameters: dict[str, Any]) -> set[str]:
    """Flatten a JSON-schema parameters dict into indexable tokens: property
    names and enum values."""
    props = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
    parts: list[str] = []
    for key, spec in props.items():
        parts.append(key)
        if isinstance(spec, dict):
            parts.extend(str(v) for v in spec.get("enum", []))
    return _tokenize(" ".join(parts))


class ToolRouter:
    """Indexes a tool registry once; scores/ranks tools per query cheaply."""

    def __init__(self, tools: dict[str, NovaTool]):
        self._tools = tools
        self._index: dict[str, dict[str, set[str]]] = {}
        for name, tool in tools.items():
            ft = tool.to_function_tool()
            self._index[name] = {
                "name": _tokenize(ft["name"]),
                "param": _param_tokens(ft["parameters"]),
                "desc": _tokenize(ft["description"]),
            }

    def score(self, query: str, name: str) -> float:
        q_tokens = _expand_synonyms(_tokenize(query))
        fields = self._index[name]
        total = 0.0
        for field, weight in (
            ("name", _NAME_WEIGHT),
            ("param", _PARAM_WEIGHT),
            ("desc", _DESC_WEIGHT),
        ):
            field_tokens = fields[field]
            exact = q_tokens & field_tokens
            total += weight * len(exact)
            total += weight * 0.5 * _substring_match_count(q_tokens - exact, field_tokens - exact)
        return total

    def top_k(
        self, query: str, k: int = 5, *, pinned: set[str] | None = None
    ) -> list[dict[str, Any]]:
        q_tokens = _expand_synonyms(_tokenize(query))

        def _rank_key(name: str) -> tuple[float, float]:
            # Tie-break equal lexical scores by how many name-tokens hit the
            # query (so web_search beats query_vehicle_status when both share
            # only the vague "current" description token).
            s = self.score(query, name)
            name_hits = float(len(q_tokens & self._index[name]["name"]))
            return (s, name_hits)

        ranked = sorted(
            self._tools.keys(),
            key=_rank_key,
            reverse=True,
        )
        pinned = pinned or set()
        # Prefer tools with score > 0 so weak/zero matches don't drown a
        # clear hit (e.g. only send_payment for a pay query). Still pad up
        # to k with lower-scoring tools so the client always gets a
        # non-empty schema list when the registry is non-empty.
        positive = [name for name in ranked if self.score(query, name) > 0]
        zeros = [name for name in ranked if name not in positive]
        pool = (positive + zeros) if positive else ranked
        pinned_ranked = [name for name in pool if name in pinned]
        # Pinned names missing from pool (should not happen) still must surface.
        for name in pinned:
            if name not in pinned_ranked and name in self._tools:
                pinned_ranked.append(name)
        if len(pinned_ranked) >= k:
            selected = pinned_ranked[:k]
        else:
            rest = [name for name in pool if name not in pinned]
            selected = pinned_ranked + rest[: k - len(pinned_ranked)]
        return [self._tools[name].to_function_tool() for name in selected]
