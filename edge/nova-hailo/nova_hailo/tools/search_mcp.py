"""FastMCP + in-process web search / deep research tools for nova-hailo.

Callers: OemToolGateway (deep_research), pipeline poll_research_job, optional stdio MCP.
Prefer in-process function calls on the Pi (no extra network hop). FastMCP registration
is optional when the `mcp` package is installed.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from nova_hailo.tools.research_jobs import (
    DEFAULT_STORE,
    POLL_INTERVAL_SEC,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_GENERATING,
    STATUS_SEARCHING,
    ResearchJob,
    ResearchJobStore,
)
from nova_hailo.tools.search_clean import (
    DEFAULT_SPEAK_CHARS,
    SearchResultCleaner,
    is_numeric_fact_query,
    literal_number,
    pick_stock_price,
    trim_to_complete_sentences,
)

TAVILY_URL = "https://api.tavily.com/search"
EXA_SEARCH_URL = "https://api.exa.ai/search"
RESEARCH_FILLER = "Looking that up."
DEFAULT_SEARCH_TIMEOUT = 1.5
# Exa contents.summary + highlights need more than the Brave snippet budget.
EXA_TIMEOUT_SEC = 8.0
# exa_search_answer() streams a synthesized answer rather than raw hits --
# slower to fully complete than the plain search (it's a generation, not a
# lookup), so it gets its own, larger floor than EXA_TIMEOUT_SEC.
EXA_ANSWER_TIMEOUT_SEC = 12.0
DEFAULT_TAVILY_TIMEOUT = 12.0


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_HEADER_RE = re.compile(r"^#{1,6}\s*", flags=re.MULTILINE)
_MD_EMPHASIS_RE = re.compile(r"[*_`]{1,3}")


def _strip_markdown(text: str) -> str:
    """Exa's raw page-text extraction can include literal markdown ('#
    Weather for...', '**bold**') that would be spoken aloud as-is by TTS.
    Brave/Serper snippets are already HTML-derived and rarely contain this,
    so applying it uniformly in _compact() is a no-op for them."""
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_HEADER_RE.sub("", text)
    return _MD_EMPHASIS_RE.sub("", text)


def _compact(title: Any, url: Any, snippet: Any) -> dict[str, str]:
    domain = ""
    try:
        domain = urlparse(str(url or "")).netloc.removeprefix("www.")[:40]
    except Exception:  # noqa: BLE001
        domain = ""
    return {
        "title": _strip_markdown(str(title or "")).strip()[:120],
        "url": domain,
        "snippet": re.sub(r"\s+", " ", _strip_markdown(str(snippet or ""))).strip()[:180],
        "source": domain,
    }


def _unavailable(name: str, reason: str, speak: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "name": name,
        "status": "unavailable",
        "reason": reason,
        "speak": speak or "I can't reach that service right now.",
        "result": None,
    }


def _ok(name: str, speak: str, result: Any = None, status: str = "success") -> dict[str, Any]:
    return {
        "ok": True,
        "name": name,
        "status": status,
        "speak": speak,
        "result": result,
    }


def _normalize_query(query: str) -> str:
    q = (query or "").strip()
    q = re.sub(
        r"(?i)\b("
        r"search(\s+the\s+web)?(\s+for)?|"
        r"look\s*up|google|research|dig\s+into|deep\s+dive|look\s+into|investigate|"
        r"(please\s+)?(can\s+you|could\s+you|nova)\s+(do|use|run)?\s*"
        r"(the\s+)?(web\s+)?search(\s+tool)?(\s+to\s+find)?|"
        r"use\s+the\s+(web\s+)?search(\s+tool)?(\s+to\s+find)?|"
        r"find\s+(me\s+)?(the\s+)?"
        r")\b",
        " ",
        q,
    )
    return re.sub(r"\s+", " ", q).strip() or (query or "").strip()


# Map common spoken company/crypto names → search-friendly "NAME TICKER" labels.
# Each entry: (match pattern, speak short label, search label).
_STOCK_NAME_TICKER: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\bamazon\b|\bamzn\b", re.I), "Amazon", "Amazon AMZN"),
    (re.compile(r"\balphabet\b|\bgoogle\b|\bgoogl\b", re.I), "Alphabet", "Alphabet GOOGL"),
    (re.compile(r"\bmicrosoft\b|\bmsft\b", re.I), "Microsoft", "Microsoft MSFT"),
    (re.compile(r"\bapple\b|\baapl\b", re.I), "Apple", "Apple AAPL"),
    (re.compile(r"\bmeta\b|\bfacebook\b", re.I), "Meta", "Meta META"),
    (re.compile(r"\btesla\b|\btsla\b", re.I), "Tesla", "Tesla TSLA"),
    (re.compile(r"\bnvidia\b|\bnvda\b", re.I), "NVIDIA", "NVIDIA NVDA"),
    (re.compile(r"\bpalantir\b|\bpltr\b", re.I), "Palantir", "Palantir PLTR"),
    (re.compile(r"\bbitcoin\b|\bbtc\b", re.I), "Bitcoin", "Bitcoin BTC-USD"),
    (re.compile(r"\bamd\b", re.I), "AMD", "AMD"),
    (re.compile(r"\bintel\b|\bintc\b", re.I), "Intel", "Intel INTC"),
)

_STOCK_KEYWORD_RE = re.compile(
    r"\b(stock|share\s+price|ticker|nasdaq|nyse)\b",
    re.I,
)
_CRYPTO_PRICE_RE = re.compile(
    r"\b(bitcoin|btc|crypto)\b.*\b(price|worth|cost|how\s+much)\b|"
    r"\b(price|worth|cost|how\s+much)\b.*\b(bitcoin|btc|crypto)\b",
    re.I,
)
_PRICE_OF_RE = re.compile(r"\bprice\s+of\b|\bhow\s+much\s+is\b|\bquote\s+for\b", re.I)
_PRICE_WORD_RE = re.compile(r"\b(price|quote|worth|cost)\b", re.I)
_NEWS_QUERY_RE = re.compile(r"\b(news|headlines|breaking)\b", re.I)


def _stock_entities_in_query(query: str) -> list[tuple[str, str, list[str]]]:
    """Entities mentioned in query, left-to-right.

    Returns list of (speak_label, search_label, near_keywords).
    """
    q = query or ""
    found: list[tuple[int, str, str, list[str]]] = []
    for pat, speak, search in _STOCK_NAME_TICKER:
        m = pat.search(q)
        if not m:
            continue
        # Keywords for proximity scoring in evidence (name + ticker tokens).
        kws = [speak] + [t for t in search.split() if t.upper() != speak.upper()]
        found.append((m.start(), speak, search, kws))
    found.sort(key=lambda t: t[0])
    # Dedupe by speak label (first occurrence wins).
    out: list[tuple[str, str, list[str]]] = []
    seen: set[str] = set()
    for _pos, speak, search, kws in found:
        key = speak.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((speak, search, kws))
    return out


def _is_stock_query(query: str) -> bool:
    """True for stock/crypto price asks that should use quote rewrite + pick."""
    q = query or ""
    if not q.strip():
        return False
    if _STOCK_KEYWORD_RE.search(q):
        return True
    if _CRYPTO_PRICE_RE.search(q):
        return True
    entities = _stock_entities_in_query(q)
    if not entities:
        return False
    if _PRICE_OF_RE.search(q) or _PRICE_WORD_RE.search(q):
        return True
    return False


def _is_multi_stock_query(query: str) -> bool:
    return len(_stock_entities_in_query(query)) >= 2


def _stock_search_query(query: str) -> str | None:
    """Rewrite stock/crypto asks so Exa/Brave rank quote pages over random $ deltas."""
    q = query or ""
    if not _is_stock_query(q):
        return None
    entities = _stock_entities_in_query(q)
    if len(entities) >= 2:
        a, b = entities[0], entities[1]
        return f"{a[1]} and {b[1]} stock price USD"
    if len(entities) == 1:
        speak, search, _kws = entities[0]
        # Crypto: "Bitcoin BTC-USD price USD" ranks better than "... stock price".
        if re.search(r"bitcoin|btc", search, re.I):
            return f"{search} price USD"
        return f"{search} stock price USD"
    return f"{q} stock price USD"


def _multi_stock_speak(query: str, evidence: str) -> str | None:
    """Build dual-price speak for multi-entity stock queries, or None if not multi."""
    entities = _stock_entities_in_query(query)
    if len(entities) < 2:
        return None
    (s1, _search1, k1), (s2, _search2, k2) = entities[0], entities[1]
    p1 = pick_stock_price(evidence, near=k1)
    p2 = pick_stock_price(evidence, near=k2, exclude=[p1] if p1 else None)
    # Do not global-fallback without near=: an unrelated $ amount would be
    # invented as the second quote (measured: oil $72.10 as "Tesla").
    # Proximity miss → honest partial for the entity we did find.
    if p1 and p2:
        return f"Search results say {s1} {p1} and {s2} {p2}."
    if p1:
        return f"Search results say {s1} {p1}. I couldn't find a reliable second price."
    if p2:
        return f"Search results say {s2} {p2}. I couldn't find a reliable second price."
    return "I couldn't find a reliable number for that."


def brave_search(
    query: str, api_key: str, timeout_sec: float = DEFAULT_SEARCH_TIMEOUT
) -> list[dict[str, str]]:
    params: dict[str, Any] = {"q": query, "count": 5, "search_lang": "en"}
    if _NEWS_QUERY_RE.search(query or ""):
        params["freshness"] = "pw"  # past week — avoid 1972-style evergreen hits
    resp = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params=params,
        headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        timeout=timeout_sec,
    )
    resp.raise_for_status()
    results = (resp.json().get("web") or {}).get("results") or []
    return [_compact(r.get("title"), r.get("url"), r.get("description")) for r in results[:5]]


def serper_search(
    query: str, api_key: str, timeout_sec: float = DEFAULT_SEARCH_TIMEOUT
) -> list[dict[str, str]]:
    resp = httpx.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": query, "num": 5},
        timeout=timeout_sec,
    )
    resp.raise_for_status()
    organic = resp.json().get("organic") or []
    return [_compact(r.get("title"), r.get("link"), r.get("snippet")) for r in organic[:5]]


def _first_highlight(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if not isinstance(raw, list) or not raw:
        return ""
    first = raw[0]
    if isinstance(first, dict):
        return str(first.get("text") or first.get("highlight") or "").strip()
    return str(first).strip()


def _exa_body(result: dict[str, Any], query: str) -> str:
    """Prefer Exa's query-aware highlights, then page text.

    Without this, news speak is the first 180 chars of raw extract — bylines,
    dates, 'min read' — the metadata-like line users hear when the Qwen
    summarizer is off. Highlights are query-relevant excerpts, so they skip
    that boilerplate.

    ``summary`` is still read first when present, but ``exa_search`` no longer
    requests it; the branch stays so any caller that does ask for one keeps
    working.
    """
    summary = _strip_markdown(str(result.get("summary") or "")).strip()
    highlight = _strip_markdown(_first_highlight(result.get("highlights")))
    text = _strip_markdown(str(result.get("text") or "")).strip()
    if _is_stock_query(query):
        return text or highlight or summary
    return summary or highlight or text


def _exa_content_query_payload(query: str, type_: str) -> dict[str, Any]:
    """Shared request body: contents/category/startPublishedDate/maxAgeHours.

    Used by both ``exa_search`` (raw hits) and ``exa_search_answer`` (Exa-side
    synthesized answer) so the two request shapes stay in lockstep -- tuning
    one (e.g. highlight length, news freshness window) tunes the other.
    """
    stockish = _is_stock_query(query)
    # Quotes need a wider excerpt so the number survives truncation.
    max_chars = 700 if stockish else 500
    contents: dict[str, Any] = {
        "text": {"maxCharacters": max_chars},
        "highlights": {"query": query, "maxCharacters": 400},
    }
    if not stockish:
        # Cache-only (never livecrawl). Livecrawl is the largest remaining
        # source of tail latency, and an article's body does not change after
        # publication -- which results come back is governed by the index and
        # startPublishedDate, not by re-fetching each page. Stock quotes are
        # the exception (the number on the page moves), so they keep the
        # default livecrawl-as-fallback behaviour.
        contents["maxAgeHours"] = -1
    payload: dict[str, Any] = {
        "query": query,
        "type": type_,
        "numResults": 5,
        "contents": contents,
    }
    if _NEWS_QUERY_RE.search(query or ""):
        payload["category"] = "news"
        # Last 7 days so "current news" is not a 1972 headline or a site about-page.
        start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00.000Z")
        payload["startPublishedDate"] = start
    return payload


def exa_search(
    query: str, api_key: str, timeout_sec: float = DEFAULT_SEARCH_TIMEOUT, type_: str = "instant"
) -> list[dict[str, str]]:
    """Raw REST call (api.exa.ai), matching brave_search/serper_search's
    style — REST only (httpx); no exa-py SDK.

    Tuned to Exa's voice-agent guidance: ``instant`` is the lowest-latency
    search type, and query-relevant ``highlights`` carry the answer in far
    fewer tokens than full page text.

    ``contents.summary`` is deliberately NOT requested. It runs an abstractive
    LLM per result on Exa's side, so its synthesis cost stacks on top of the
    base search type. The spoken answer is composed by the pipeline's own LLM
    round anyway, making the summary duplicated work — and it prefixed bodies
    with literal "Summary:" / "Key points:" markers that reached the TTS.
    """
    stockish = _is_stock_query(query)
    payload = _exa_content_query_payload(query, type_)
    resp = httpx.post(
        EXA_SEARCH_URL,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=timeout_sec,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    out: list[dict[str, str]] = []
    for r in results[:5]:
        if not isinstance(r, dict):
            continue
        body = _exa_body(r, query)
        c = _compact(r.get("title"), r.get("url"), body)
        # Keep more of Exa's body than Brave's 180-char snippet ceiling.
        if body:
            c["snippet"] = re.sub(r"\s+", " ", body).strip()[: 480 if stockish else 400]
        out.append(c)
    return out


def exa_search_answer(
    query: str,
    api_key: str,
    *,
    system_prompt: str,
    timeout_sec: float = EXA_ANSWER_TIMEOUT_SEC,
    type_: str = "instant",
    on_token: Callable[[str], None] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Exa /search with outputSchema + systemPrompt + stream:true.

    Exa synthesizes the spoken answer itself instead of returning raw hits
    for a second LLM round to compose from. It streams back OpenAI-compatible
    SSE -- ``choices[0].delta.content`` per piece -- the same shape
    ``OpenRouterLLM.generate()`` already parses, so ``on_token`` can be the
    pipeline's existing token_cb and feed straight into the same
    first_complete_clause -> _emit_validated_clause -> TTS path.

    Measured on this Pi: type=instant reaches the first synthesized token in
    ~988ms, replacing an Exa search (~910ms) plus a second OpenRouter LLM
    call's TTFT (~650ms) -- about 1560ms today, one whole round trip fewer.

    Raises on any transport/HTTP failure -- no swallowing here. Same split as
    exa_search()/web_search(): the low-level call raises, the wrapper
    (web_search_answer) is what fails closed.
    """
    payload = _exa_content_query_payload(query, type_)
    payload["stream"] = True
    payload["systemPrompt"] = system_prompt
    payload["outputSchema"] = {
        "type": "text",
        "description": "One short spoken sentence answering the question.",
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    text_parts: list[str] = []
    grounding: Any = None

    def _consume(resp: httpx.Response) -> None:
        nonlocal grounding
        if resp.status_code >= 400:
            body = resp.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"Exa HTTP {resp.status_code}: {body}")
        for line in resp.iter_lines():
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            if line.startswith(":") or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    text_parts.append(piece)
                    if on_token is not None:
                        on_token(piece)
            out_field = chunk.get("output")
            if isinstance(out_field, dict) and out_field.get("grounding") is not None:
                grounding = out_field.get("grounding")

    if client is not None:
        with client.stream(
            "POST", EXA_SEARCH_URL, headers=headers, json=payload, timeout=timeout_sec
        ) as resp:
            _consume(resp)
    else:
        with httpx.stream(
            "POST", EXA_SEARCH_URL, headers=headers, json=payload, timeout=timeout_sec
        ) as resp:
            _consume(resp)

    return {"text": "".join(text_parts).strip(), "grounding": grounding}


def answer_from_hits(query: str, hits: list, *, tool_name: str = "web_search") -> dict[str, Any]:
    cleaner = SearchResultCleaner()
    evidence = cleaner.evidence_block(hits)
    if not evidence:
        return _unavailable(tool_name, "no_usable_snippets")
    stockish = _is_stock_query(query)
    if is_numeric_fact_query(query) or stockish:
        if _is_multi_stock_query(query):
            speak = _multi_stock_speak(query, evidence)
            return _ok(
                tool_name,
                speak or "I couldn't find a reliable number for that.",
                {"evidence": evidence, "numeric": True, "needs_summary": False},
            )
        if stockish:
            num = pick_stock_price(evidence)
        else:
            num = literal_number(evidence, query=query)
        if num:
            speak = f"Search results say {num}."
        else:
            speak = "I couldn't find a reliable number for that."
        return _ok(
            tool_name,
            speak,
            {"evidence": evidence, "numeric": True, "needs_summary": False},
        )
    # News / general: speak Exa summary/highlight (or first clean snippet).
    # Qwen summarizer is opt-in via tools.summarize_search (currently off).
    return _ok(
        tool_name,
        cleaner.speak_fallback(hits),
        {"evidence": evidence, "numeric": False, "needs_summary": True},
    )


def web_search(
    query: str,
    *,
    timeout_sec: float = DEFAULT_SEARCH_TIMEOUT,
    serper_fallback: bool = False,
) -> dict[str, Any]:
    """Exa only (type=instant + highlights/text). No Brave/Serper.

    ``serper_fallback`` is ignored — kept so ToolBroker's kwargs stay valid.
    Fail closed if EXA_API_KEY is missing or the Exa call fails/times out.
    """
    del serper_fallback  # Exa-only; never fall through to Brave/Serper.
    q = _normalize_query(query)
    if not q:
        return _unavailable("web_search", "empty_query")
    search_q = _stock_search_query(q) or q
    exa = (os.environ.get("EXA_API_KEY") or "").strip()
    if not exa:
        return _unavailable("web_search", "no_exa_api_key")
    try:
        hits = exa_search(
            search_q,
            exa,
            timeout_sec=max(float(timeout_sec), EXA_TIMEOUT_SEC),
            type_="instant",
        )
    except Exception as exc:  # noqa: BLE001
        return _unavailable("web_search", f"exa_failed:{exc}")
    if not hits:
        return _unavailable("web_search", "exa_empty")
    out = answer_from_hits(q, hits)
    if isinstance(out.get("result"), dict):
        out["result"]["provider"] = "exa"
    return out


def web_search_answer(
    query: str,
    *,
    system_prompt: str,
    timeout_sec: float = DEFAULT_SEARCH_TIMEOUT,
    on_token: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Exa-synthesized spoken answer, streamed via ``on_token`` as it arrives.

    Config-gated alternate to web_search() + a second LLM round (see
    pipeline._openrouter_turn's exa_direct_answer branch): Exa answers the
    question itself (type=instant + outputSchema + stream) instead of Nova's
    own LLM composing from raw hits, removing that second round trip. Same
    fail-closed contract as web_search(): a missing EXA_API_KEY or a failed
    Exa call returns ok=False/unavailable rather than raising into the
    pipeline.
    """
    q = _normalize_query(query)
    if not q:
        return _unavailable("web_search", "empty_query")
    search_q = _stock_search_query(q) or q
    exa = (os.environ.get("EXA_API_KEY") or "").strip()
    if not exa:
        return _unavailable("web_search", "no_exa_api_key")
    try:
        result = exa_search_answer(
            search_q,
            exa,
            system_prompt=system_prompt,
            timeout_sec=max(float(timeout_sec), EXA_ANSWER_TIMEOUT_SEC),
            type_="instant",
            on_token=on_token,
        )
    except Exception as exc:  # noqa: BLE001
        return _unavailable("web_search", f"exa_answer_failed:{exc}")
    text = str(result.get("text") or "").strip()
    if not text:
        return _unavailable("web_search", "exa_answer_empty")
    out = _ok(
        "web_search",
        text,
        {"provider": "exa_direct", "grounding": result.get("grounding")},
    )
    return out


def tavily_advanced_search(
    query: str,
    api_key: str,
    *,
    max_results: int = 5,
    timeout_sec: float = DEFAULT_TAVILY_TIMEOUT,
) -> dict[str, Any]:
    resp = httpx.post(
        TAVILY_URL,
        json={
            "query": query,
            "search_depth": "advanced",
            "chunks_per_source": 3,
            "max_results": max_results,
            "include_answer": "advanced",
        },
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout_sec,
    )
    resp.raise_for_status()
    data = resp.json()
    sources = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", "") or r.get("snippet", ""),
            "source": "",
        }
        for r in data.get("results", [])
    ]
    for s in sources:
        try:
            s["source"] = urlparse(str(s.get("url") or "")).netloc.removeprefix("www.")[:40]
            s["url"] = s["source"]
        except Exception:  # noqa: BLE001
            pass
    return {
        "answer": str(data.get("answer") or "").strip(),
        "sources": sources,
    }


def _research_worker(store: ResearchJobStore, job: ResearchJob, timeout_sec: float) -> None:
    api_key = (os.environ.get("TAVILY_API_KEY") or "").strip()
    if not api_key:
        store.update(
            job.job_id,
            status=STATUS_FAILED,
            speak="I can't reach that service right now.",
            reason="no_tavily_api_key",
        )
        return
    store.update(job.job_id, status=STATUS_SEARCHING)
    try:
        payload = tavily_advanced_search(job.query, api_key, timeout_sec=timeout_sec)
    except Exception as exc:  # noqa: BLE001
        store.update(
            job.job_id,
            status=STATUS_FAILED,
            speak="I couldn't finish that research.",
            reason=f"tavily_failed:{exc}",
        )
        return

    store.update(job.job_id, status=STATUS_GENERATING)
    answer = str(payload.get("answer") or "").strip()
    sources = list(payload.get("sources") or [])
    cleaner = SearchResultCleaner()
    evidence = cleaner.evidence_block(sources) if sources else ""
    if answer:
        speak = trim_to_complete_sentences(
            re.sub(r"\s+", " ", answer).strip(), DEFAULT_SPEAK_CHARS
        )
    elif evidence:
        speak = cleaner.speak_fallback(sources)
    else:
        store.update(
            job.job_id,
            status=STATUS_FAILED,
            speak="I couldn't find enough sources for that.",
            reason="empty_tavily",
        )
        return
    # Provisional speak = Tavily/cleaner fallback; pipeline may summarize evidence.
    store.update(
        job.job_id,
        status=STATUS_DONE,
        speak=speak,
        evidence=evidence or answer[:700],
    )


def deep_research(
    query: str,
    *,
    store: ResearchJobStore | None = None,
    timeout_sec: float = DEFAULT_TAVILY_TIMEOUT,
) -> dict[str, Any]:
    """Start async Tavily research; returns filler speak + job_id immediately."""
    q = _normalize_query(query)
    if not q:
        return _unavailable("deep_research", "empty_query")
    api_key = (os.environ.get("TAVILY_API_KEY") or "").strip()
    if not api_key:
        return _unavailable("deep_research", "no_tavily_api_key")

    job_store = store or DEFAULT_STORE
    job, reason = job_store.try_begin(q)
    if job is None:
        return _unavailable(
            "deep_research",
            reason or "research_busy",
            speak="I'm already looking something up — give me a moment.",
        )

    job_store.start_worker(
        job,
        lambda j: _research_worker(job_store, j, timeout_sec),
    )
    return _ok(
        "deep_research",
        RESEARCH_FILLER,
        {"job_id": job.job_id, "status": STATUS_SEARCHING},
        status=STATUS_SEARCHING,
    )


def research_status(
    job_id: str,
    *,
    store: ResearchJobStore | None = None,
) -> dict[str, Any]:
    job_store = store or DEFAULT_STORE
    job = job_store.get(job_id)
    if job is None:
        return {
            "ok": False,
            "name": "research_status",
            "status": STATUS_FAILED,
            "reason": "unknown_job",
            "speak": "I lost track of that lookup.",
            "result": None,
        }
    payload = job.as_dict()
    terminal = job.status in {STATUS_DONE, STATUS_FAILED}
    needs_summary = bool(
        job.status == STATUS_DONE and (job.evidence or "").strip()
    )
    if needs_summary:
        payload["needs_summary"] = True
        payload["numeric"] = False
    return {
        "ok": terminal and job.status == STATUS_DONE,
        "name": "research_status",
        "status": job.status,
        "speak": job.speak
        or (
            RESEARCH_FILLER
            if job.status in {STATUS_SEARCHING, STATUS_GENERATING}
            else "I couldn't finish that research."
        ),
        "result": payload,
        "evidence": job.evidence or None,
        "reason": job.reason or None,
    }


def build_fastmcp_app(store: ResearchJobStore | None = None):
    """Optional FastMCP server; returns None if `mcp` is not installed."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        return None

    job_store = store or DEFAULT_STORE
    app = FastMCP("nova-hailo-search")

    @app.tool()
    def web_search_tool(query: str) -> dict[str, Any]:
        """Quick web lookup (Brave → Serper)."""
        return web_search(query)

    @app.tool()
    def deep_research_tool(query: str) -> dict[str, Any]:
        """Start deeper multi-source research (async job)."""
        return deep_research(query, store=job_store)

    @app.tool()
    def research_status_tool(job_id: str) -> dict[str, Any]:
        """Poll a deep_research job: searching | generating | done | failed."""
        return research_status(job_id, store=job_store)

    return app


__all__ = [
    "RESEARCH_FILLER",
    "POLL_INTERVAL_SEC",
    "web_search",
    "web_search_answer",
    "exa_search",
    "exa_search_answer",
    "deep_research",
    "research_status",
    "build_fastmcp_app",
    "answer_from_hits",
    "_stock_search_query",
    "_is_stock_query",
    "_stock_entities_in_query",
    "_multi_stock_speak",
]
