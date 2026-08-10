"""Deterministic route -> canonical intent (SCENIC pattern, ROADMAP.md §2).

Callers: nova_hailo.edge_harness.tool_broker (imports the *_speak helpers);
nova_hailo.tools.oem_tools (facade).

Moved from OemToolGateway.route()/_smalltalk_speak()/_unavailable_cap_speak()
verbatim -- same regexes, same precedence order, same measured-bug fixes
(anchoring, plural matching, falsy-default enabled set). This is a
representation change (bare string -> typed RoutedIntent), not a routing
logic rewrite; every existing router test must still pass unchanged.
"""
from __future__ import annotations

import re

from nova_hailo.edge_harness.policy import CapabilityProfile
from nova_hailo.edge_harness.types import Intent, RoutedIntent

_CAL_RE = re.compile(
    r"\b(calendar|schedule|meetings?|what's on|whats on|agenda|appointments?)\b", re.I
)
_MAIL_RE = re.compile(r"\b(email|inbox|gmail|mail from|unread)\b", re.I)
_DRIVE_RE = re.compile(r"\b(drive|files?|documents?|docs?|folder)\b", re.I)
# Explicit search / lookup intents only -- do not match casual "what is your name?".
# Bare "news" / "current news today" must match -- otherwise the chat LLM + soul.md
# ("no internet") refuses and identity-drifts (measured 2026-08-04 live session).
_WEB_RE = re.compile(
    r"\b("
    r"search(\s+the\s+web|\s+online|\s+for)?|"
    r"look\s*up|google|"
    r"find(\s+(me|this|a|the|that))?\s+(company|startup|firm)|"
    r"company\s+named|"
    r"(current|latest|today'?s?|breaking)\s+news|"
    r"news(\s+(about|on|in|from|today|latest))?|"
    r"headlines?|"
    r"who\s+won|weather|forecast|"
    # Stock/share price + common ASR garble (pressure/stoppage/stop press)
    r"stock\s+(prices?|pressure|press|quote)|share\s+prices?|"
    r"stop\s*press|stoppage|"
    r"(price|quote)\s+of\s+\w+|"
    r"(ticker|nasdaq|nyse)\b|"
    r"(current|latest)\s+status|status\s+of|"
    r"situation\s+(in|with|of)"
    r")\b",
    re.I,
)
# Shared stock/crypto entity alts (multi + bare + follow-up "X only").
_STOCK_ENTITY_ALTS = (
    r"apple|alphabet|google|microsoft|meta|amazon|tesla|nvidia|amd|intel|"
    r"palantir|pltr|bitcoin|btc|"
    r"tsla|googl|msft|aapl|amzn|nvda|fb|meta\s+platforms"
)
# Follow-up after a stock turn — broad enough for ASR garble:
# "How the Microsoft", "how about Meta", "and Apple?", "only Nvidia"
# Named groups avoid off-by-one from nested alts.
_STOCK_FOLLOWUP_RE = re.compile(
    r"^\s*(?:"
    r"(?:"
    r"how\s+(?:about|the|is)|what\s+about|and|about(?:\s+the)?|only"
    r")\s+(?:the\s+)?(?P<entity>[A-Za-z][\w&.-]*)\s*\??\s*$"
    r"|"
    # "Nvidia only" / "the Tesla only"
    r"(?:the\s+)?(?P<entity_only>[A-Za-z][\w&.-]*)\s+only\s*\??\s*$"
    r")",
    re.I,
)
# Stock/price signal may appear before OR after the two entities
# ("stock price of NVIDIA and Tesla" / "NVIDIA and Tesla stock").
_STOCK_SIGNAL_RE = re.compile(
    r"\b(?:stock|share|price|quote|press|pressure|stoppage|stop\s*press)\b",
    re.I,
)
_MULTI_STOCK_RE = re.compile(
    r"\b(?:stock|share|price|quote|press|pressure|stoppage|stop\s*press)\b.{0,40}\b"
    r"(?P<a>" + _STOCK_ENTITY_ALTS + r")\b"
    r".{0,24}(?:\b(?:and|on|plus)\b|&).{0,24}\b"
    r"(?P<b>" + _STOCK_ENTITY_ALTS + r")\b",
    re.I,
)
# Two known stock entities joined by and/&; requires a stock signal anywhere.
_MULTI_STOCK_ENTITIES_RE = re.compile(
    r"\b(?P<a>" + _STOCK_ENTITY_ALTS + r")\b"
    r".{0,24}(?:\b(?:and|plus)\b|&).{0,24}\b"
    r"(?P<b>" + _STOCK_ENTITY_ALTS + r")\b",
    re.I,
)
_STOCK_ENTITY_RE = re.compile(
    r"\b(" + _STOCK_ENTITY_ALTS + r")\b",
    re.I,
)
_COMPANY_LOOKUP_RE = re.compile(
    r"\b(?:find|look\s*up|search\s+for|who\s+is|what\s+is)\b.{0,48}?"
    r"(?:company|startup|firm)\b(?:\s+(?:named|called))?\s+"
    r"(?P<name>[A-Za-z][\w .&-]{1,48})",
    re.I,
)
_RESEARCH_RE = re.compile(
    r"\b(research|dig\s+into|deep\s+dive|look\s+into|investigate)\b",
    re.I,
)
_CHAT_BLOCK_WEB = re.compile(
    r"\b(your\s+name|who\s+are\s+you|what\s+can\s+you\s+do|introduce\s+yourself)\b",
    re.I,
)
# Identity/capability turns answer deterministically: a 1.5B model's own identity
# reply is what seeds the in-context repetition lock (measured 2026-07-29), and the
# canned path also drops these turns from ~2.1s to ~0.2s TTFA.
_IDENTITY_RE = re.compile(
    r"\b(your\s+name|who\s+are\s+you|who\s+is\s+nova|what\s+are\s+you|"
    r"introduce\s+yourself|surname|what'?s\s+your\s+name)\b",
    re.I,
)
_CAPABILITY_RE = re.compile(
    r"\b(what\s+can\s+you\s+do|what\s+do\s+you\s+do|how\s+can\s+you\s+help|"
    r"what\s+else\s+can\s+you\s+do|your\s+capabilit(y|ies)|"
    r"what\s+are\s+your\s+capabilit)\b",
    re.I,
)
# Bare clarifiers after a bad/misheard turn — do not let the LLM free-chat.
# Whole-utterance only (anchors): "no problem" / "not really" must not match.
_CLARIFY_RE = re.compile(
    r"^\s*(what|huh|pardon|sorry|come\s+again|say\s+again|repeat|"
    r"nope|nah|no|n)\s*[?.!]?\s*$",
    re.I,
)
CLARIFY_SPEAK = "Could you say that again?"

# Never let the 1.5B paraphrase or leak soul.md (measured live 2026-08-07).
_SYSTEM_PROMPT_RE = re.compile(
    r"\b("
    r"(share|show|reveal|print|dump|read|tell\s+me)\s+(me\s+)?"
    r"(your\s+)?(system\s+)?(prompt|instructions?|system\s+message)|"
    r"what\s+(is|are)\s+your\s+(system\s+)?(prompt|instructions?)|"
    r"share\s+your\s+system"
    r")\b",
    re.I,
)
SYSTEM_PROMPT_SPEAK = "I can't share that."
CAPABILITY_SPEAK = (
    "I can chat, look up the web, and check calendar, email, and Drive when connected."
)
IDENTITY_SPEAK = "I'm Nova, your car's built-in assistant. Happy to keep you company."

# Force web_search for "who holds this office now" — free-chat invents names
# (measured: UK PM → Sunak / Andy Burnham, 2026-08-08/09 live).
_OFFICE_ALTS = (
    r"prime\s+minister|p\.?m\.?|chancellor|president|head\s+of\s+government"
)
_COUNTRY_NAME_ALTS = (
    r"united\s+kingdom|great\s+britain|britain|u\.?k\.?|"
    r"united\s+states(?:\s+of\s+america)?|u\.?s\.?a\.?|u\.?s\.?|"
    r"germany|france|india"
)
_COUNTRY_ADJ_ALTS = r"uk|british|german|french|indian|american|us"
# (1) [who is] [the] [current] <office> of/in [the] <country>
# (2) [who is] [the] [current] <adj> <office>   e.g. German chancellor
# Require present-tense "who is" and/or explicit "current" so historical
# "who was PM in 2010" does not force search.
_CURRENT_LEADER_RE = re.compile(
    r"\b(?:"
    r"(?:who(?:'s|\s+is)\s+)?(?:the\s+)?(?:current\s+)?"
    r"(?P<office1>" + _OFFICE_ALTS + r")"
    r"\s+(?:of|in)\s+(?:the\s+)?(?P<country1>" + _COUNTRY_NAME_ALTS + r")"
    r"|"
    r"(?:who(?:'s|\s+is)\s+)?(?:the\s+)?(?:current\s+)?"
    r"(?P<country_adj>" + _COUNTRY_ADJ_ALTS + r")"
    r"\s+(?P<office2>" + _OFFICE_ALTS + r")"
    r"|"
    r"current\s+(?P<office3>" + _OFFICE_ALTS + r")"
    r"\s+(?:of|in)\s+(?:the\s+)?(?P<country3>" + _COUNTRY_NAME_ALTS + r")"
    r")\b",
    re.I,
)
# Guard: only force-search when utterance signals present office-holder.
_LEADER_PRESENT_RE = re.compile(
    r"\b(current|who(?:'s|\s+is)|today|now|right\s+now)\b",
    re.I,
)
_COUNTRY_CANON: dict[str, str] = {
    "uk": "the United Kingdom",
    "u.k": "the United Kingdom",
    "u.k.": "the United Kingdom",
    "united kingdom": "the United Kingdom",
    "britain": "the United Kingdom",
    "great britain": "the United Kingdom",
    "british": "the United Kingdom",
    "germany": "Germany",
    "german": "Germany",
    "france": "France",
    "french": "France",
    "india": "India",
    "indian": "India",
    "us": "the United States",
    "usa": "the United States",
    "u.s": "the United States",
    "u.s.": "the United States",
    "u.s.a": "the United States",
    "u.s.a.": "the United States",
    "united states": "the United States",
    "united states of america": "the United States",
    "american": "the United States",
}
_OFFICE_CANON: dict[str, str] = {
    "prime minister": "prime minister",
    "pm": "prime minister",
    "p.m": "prime minister",
    "p.m.": "prime minister",
    "chancellor": "chancellor",
    "president": "president",
    "head of government": "head of government",
}

# Meta-tool phrasing with a clear object: "use the web search tool to find X".
# Pure "search again" / "use the search tool" with no object → Task 2 (stateful).
_META_SEARCH_RE = re.compile(
    r"\b(?:please\s+)?(?:can\s+you\s+)?"
    r"(?:use\s+(?:the\s+)?(?:web\s+)?search(?:\s+tool)?|try\s+searching)"
    r"\s+(?:to\s+)?(?:find|look\s*up|search\s+for)\s+(?P<object>.+?)\s*$",
    re.I,
)
# Whole-utterance pure meta (no object) — do not WEB_RE-match "search" here;
# Task 2 re-runs last tool when session has prior search intent.
_PURE_META_SEARCH_RE = re.compile(
    r"^\s*(?:please\s+)?(?:can\s+you\s+)?"
    r"(?:"
    r"use\s+(?:the\s+)?(?:web\s+)?search(?:\s+tool)?(?:\s+again)?"
    r"|try\s+searching(?:\s+again)?"
    r"|search\s+again(?:\s+please)?"
    r"|look\s+it\s+up\s+again"
    r")\s*[?.!]*\s*$",
    re.I,
)
# Explicit "again" / re-run phrasing (includes bare "try again").
_SEARCH_AGAIN_RE = re.compile(
    r"^\s*(?:please\s+)?(?:can\s+you\s+)?"
    r"(?:"
    r"search\s+again(?:\s+please)?"
    r"|try\s+again(?:\s+please)?"
    r"|try\s+searching\s+again"
    r"|look\s+(?:it\s+)?up\s+again"
    r"|use\s+(?:the\s+)?(?:web\s+)?search(?:\s+tool)?\s+again"
    r")\s*[?.!]*\s*$",
    re.I,
)


def is_search_again(query: str) -> bool:
    """True for pure meta re-run phrasing: search/try/look-up again, use search tool.

    Used by OemToolGateway.route_and_execute before normal route when session
    has a prior web_search / deep_research intent.
    """
    q = (query or "").strip()
    if not q:
        return False
    return bool(_SEARCH_AGAIN_RE.match(q) or _PURE_META_SEARCH_RE.match(q))

# Smalltalk answered deterministically: highest-frequency demo turns, and every
# one removed from the LLM path drops ~3.5s to <0.8s and cannot repetition-lock.
_SMALLTALK: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # Whole-utterance only. A prefix match swallowed real questions:
        # "Hey there can we chat about the latest BMW?" answered "Hello."
        re.compile(
            r"^\s*(hi|hey|hello|yo|good\s+(morning|afternoon|evening))"
            r"[\s,!.]*(there|nova|buddy)?[\s,!.]*$",
            re.I,
        ),
        "Hello. How can I help?",
    ),
    (
        re.compile(r"\b(thanks|thank\s+you|cheers|appreciate\s+it)\b", re.I),
        "Anytime.",
    ),
    (
        re.compile(r"\b(goodbye|bye|see\s+you|good\s*night|that'?s\s+all)\b", re.I),
        "Goodbye. Talk soon.",
    ),
    (
        # Whole-utterance (with an optional short prefix/suffix), not a
        # substring match. "stop"/"cancel" are common English words, and a
        # bare \b match swallowed real questions: garbled ASR turned "stock
        # price of Tesla" into "stop price of Tesla" and this fired
        # "Okay, stopping." instead of routing to search (measured 2026-08-04).
        re.compile(
            r"^\s*(hey\s+nova|hey|nova|ok(ay)?)?[\s,!.]*"
            r"(stop|cancel|never\s*mind|forget\s+it)"
            r"(\s+(that|it|please|now))?[\s,!.]*$",
            re.I,
        ),
        "Okay, stopping.",
    ),
)

# Capability asks with no implementation in this profile. A 1.5B cannot be
# prompted into reliably declining these -- measured 2026-07-30 it invented a
# clock time, a fake email result and a false reminder promise. Each entry is
# (pattern, required_tool, honest_reply); the decline only fires when that tool
# is NOT enabled, so it never shadows a real tool in oem_readonly.
_UNAVAILABLE_CAPS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\b(what|whats|what's)\s+(the\s+)?(time|date|day)\b", re.I),
     "get_datetime", "I can't read the clock yet, sorry."),
    (re.compile(r"\b(weather|forecast|raining|temperature)\b", re.I),
     "web_search", "I can't check the weather yet."),
    (re.compile(r"\b(traffic|route\s+to|navigate)\b", re.I),
     "navigation", "I can't check traffic or routes yet."),
    (re.compile(r"\b(email|inbox|gmail)\b", re.I),
     "check_email", "I can't get into your email yet."),
    (re.compile(r"\b(calendar|schedule|meetings?|agenda|appointments?)\b", re.I),
     "check_calendar", "I can't see your calendar yet."),
    (re.compile(r"\b(remind\s+me|reminder)\b", re.I),
     "create_reminder", "I can't set reminders yet, but I'm listening."),
    (re.compile(r"\b(news|headlines|stock|share\s+price)\b", re.I),
     "web_search", "I can't look that up yet."),
    (re.compile(r"\b(search\s+the\s+web|google\s+it|look\s+it\s+up)\b", re.I),
     "web_search", "I can't search the web yet."),
    (re.compile(r"\b(research|dig\s+into|deep\s+dive|look\s+into|investigate)\b", re.I),
     "deep_research", "I can't do deep research yet."),
)


def _unavailable_cap_speak(query: str, enabled: frozenset[str]) -> str | None:
    """Honest decline for a capability this profile does not implement."""
    for pattern, tool, speak in _UNAVAILABLE_CAPS:
        if tool not in enabled and pattern.search(query):
            return speak
    return None


def _smalltalk_speak(query: str) -> str | None:
    for pattern, speak in _SMALLTALK:
        if pattern.search(query):
            return speak
    return None


def calendar_day_slot(query: str) -> str:
    """Canonical day-window slot for check_calendar, resolved at route time
    instead of deep inside the tool call (SCENIC: pick intent, fill slots)."""
    q = (query or "").lower()
    if "tomorrow" in q:
        return "tomorrow"
    if "today" in q or "tonight" in q:
        return "today"
    return "week"


def _normalize_office(raw: str) -> str | None:
    key = re.sub(r"\s+", " ", (raw or "").strip().lower().rstrip("."))
    return _OFFICE_CANON.get(key)


def _normalize_country(raw: str) -> str | None:
    key = re.sub(r"\s+", " ", (raw or "").strip().lower().rstrip("."))
    return _COUNTRY_CANON.get(key)


def _current_leader_query(query: str) -> str | None:
    """Rewrite current office-holder asks into a canonical web_search query.

    Live: free-chat invents UK PM / chancellor names; force search instead.
    """
    q = (query or "").strip()
    if not q or not _LEADER_PRESENT_RE.search(q):
        return None
    # Historical past tense: leave alone.
    if re.search(r"\bwho\s+was\b", q, re.I):
        return None
    m = _CURRENT_LEADER_RE.search(q)
    if not m:
        return None
    office_raw = m.group("office1") or m.group("office2") or m.group("office3") or ""
    country_raw = (
        m.group("country1") or m.group("country_adj") or m.group("country3") or ""
    )
    office = _normalize_office(office_raw)
    country = _normalize_country(country_raw)
    if not office or not country:
        return None
    return f"current {office} of {country}"


def _meta_search_object(query: str) -> str | None:
    """Strip 'use the search tool to find …' meta phrasing; return object or None.

    No clear object → None (stateful 'search again' is Task 2).
    """
    m = _META_SEARCH_RE.search(query or "")
    if not m:
        return None
    obj = (m.group("object") or "").strip(" ?.!,")
    if len(obj) < 2:
        return None
    return obj


def _stock_followup_query(query: str) -> str | None:
    """Expand short coref / ASR-garbled stock follow-ups into a price search.

    Live: "About the Microsoft." / "How the Microsoft" after Amazon fell
    through to a company blurb instead of web_search.
    Also: "Nvidia only" / "only Nvidia" → stock price of nvidia.
    """
    q = (query or "").strip()
    multi = _MULTI_STOCK_RE.search(q)
    if not multi:
        # Trailing/anywhere stock signal: "NVIDIA and Tesla stock"
        multi = _MULTI_STOCK_ENTITIES_RE.search(q)
        if multi and not _STOCK_SIGNAL_RE.search(q):
            multi = None
    if multi:
        return f"stock price of {multi.group('a')} and {multi.group('b')}"
    m = _STOCK_FOLLOWUP_RE.match(q)
    if m:
        entity = (m.group("entity") or m.group("entity_only") or "").strip(" ?.!,")
        if entity and len(entity) >= 2:
            # "X only" form: only treat known stock/crypto entities as price asks
            # so casual English ("me only") does not force web_search.
            if m.group("entity_only") is not None:
                if not _STOCK_ENTITY_RE.fullmatch(entity):
                    return None
            # "only X": require known entity for the same reason.
            if re.match(r"^\s*only\b", q, re.I) and not _STOCK_ENTITY_RE.fullmatch(
                entity
            ):
                return None
            return f"stock price of {entity}"
    bare = q.strip(" ?.!,")
    if _STOCK_ENTITY_RE.fullmatch(bare):
        return f"stock price of {bare}"
    return None


def _company_lookup_query(query: str) -> str | None:
    m = _COMPANY_LOOKUP_RE.search(query or "")
    if not m:
        return None
    name = (m.group("name") or "").strip(" ?.!,")
    if len(name) < 2:
        return None
    return f"company {name}"


def route(query: str, profile: CapabilityProfile) -> RoutedIntent | None:
    q = (query or "").strip()
    if not q:
        return None
    # Identity first: never let the LLM author its own identity line.
    if _IDENTITY_RE.search(q):
        return RoutedIntent(Intent.IDENTITY, q)
    if _SYSTEM_PROMPT_RE.search(q):
        return RoutedIntent(Intent.SMALLTALK, q, {"speak": SYSTEM_PROMPT_SPEAK})
    if _CLARIFY_RE.match(q):
        return RoutedIntent(Intent.SMALLTALK, q, {"speak": CLARIFY_SPEAK})
    if _CAPABILITY_RE.search(q):
        return RoutedIntent(Intent.CAPABILITY, q)
    if _smalltalk_speak(q):
        return RoutedIntent(Intent.SMALLTALK, q)
    if _unavailable_cap_speak(q, profile.enabled):
        return RoutedIntent(Intent.CAPABILITY_UNAVAILABLE, q)
    if profile.allows(Intent.CHECK_CALENDAR) and _CAL_RE.search(q):
        return RoutedIntent(Intent.CHECK_CALENDAR, q, {"day": calendar_day_slot(q)})
    if profile.allows(Intent.CHECK_EMAIL) and _MAIL_RE.search(q):
        return RoutedIntent(Intent.CHECK_EMAIL, q)
    if profile.allows(Intent.LIST_DRIVE_FILES) and _DRIVE_RE.search(q):
        return RoutedIntent(Intent.LIST_DRIVE_FILES, q)
    # Research before quick search so "research X" never collapses to web_search.
    if (
        profile.allows(Intent.DEEP_RESEARCH)
        and _RESEARCH_RE.search(q)
        and not _CHAT_BLOCK_WEB.search(q)
    ):
        return RoutedIntent(Intent.DEEP_RESEARCH, q)
    # Stock follow-up / multi-entity / bare entity / "Nvidia only" — before generic web.
    stock_q = _stock_followup_query(q)
    if profile.allows(Intent.WEB_SEARCH) and stock_q and not _CHAT_BLOCK_WEB.search(q):
        return RoutedIntent(Intent.WEB_SEARCH, stock_q)
    # Meta-tool with clear object: strip to remainder, then leader rewrite / search.
    meta_obj = _meta_search_object(q)
    search_base = meta_obj if meta_obj is not None else q
    # Current leaders: force WEB_SEARCH with canonical rewrite (never free-chat).
    leader_q = _current_leader_query(search_base)
    if profile.allows(Intent.WEB_SEARCH) and leader_q and not _CHAT_BLOCK_WEB.search(q):
        return RoutedIntent(Intent.WEB_SEARCH, leader_q)
    if (
        meta_obj is not None
        and profile.allows(Intent.WEB_SEARCH)
        and not _CHAT_BLOCK_WEB.search(q)
    ):
        stock_meta = _stock_followup_query(meta_obj)
        if stock_meta:
            return RoutedIntent(Intent.WEB_SEARCH, stock_meta)
        company_meta = _company_lookup_query(meta_obj)
        if company_meta:
            return RoutedIntent(Intent.WEB_SEARCH, company_meta)
        return RoutedIntent(Intent.WEB_SEARCH, meta_obj)
    company_q = _company_lookup_query(q)
    if profile.allows(Intent.WEB_SEARCH) and company_q and not _CHAT_BLOCK_WEB.search(q):
        return RoutedIntent(Intent.WEB_SEARCH, company_q)
    if _PURE_META_SEARCH_RE.match(q):
        # Stateful re-run is Task 2; avoid searching the meta phrase itself.
        return None
    if profile.allows(Intent.WEB_SEARCH) and _WEB_RE.search(q) and not _CHAT_BLOCK_WEB.search(q):
        return RoutedIntent(Intent.WEB_SEARCH, q)
    return None
