"""General web search, exposed as a ``NovaTool``.

Brave Search is the primary provider; on ANY failure (missing key, HTTP
error, timeout, malformed response) it falls back to Serper in the same call
— a single round-trip from the model's point of view. If neither provider is
usable, ``execute()`` returns a clear "search unavailable" result rather than
raising, so a bad/missing key never crashes the voice loop.

News mode: optional ``category`` / ``place`` fan out across a small topic
spectrum (politics, traffic, business, sports, crime) via news endpoints,
dedupe aggregator junk, and return a short ``speak`` field so LFM can read
headlines without stuffing the 16k context with URLs.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx

from nova.tools._env import get_api_key
from nova.tools.base import NovaTool

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_NEWS_URL = "https://api.search.brave.com/res/v1/news/search"
SERPER_URL = "https://google.serper.dev/search"
SERPER_NEWS_URL = "https://google.serper.dev/news"

_UNSET: Any = object()

# Spectrum covered for bare "news in <place>" — one headline each keeps context tiny.
NEWS_SPECTRUM: tuple[str, ...] = (
    "politics",
    "traffic",
    "business",
    "sports",
    "crime",
)
CATEGORY_QUERY: dict[str, str] = {
    "politics": "politics government",
    "traffic": "traffic transport metro",
    "business": "business economy",
    "sports": "sports cricket",
    "crime": "crime police",
    "tech": "technology startups",
    "weather": "weather rain flood",
    "general": "news headlines today",
}
_AGGREGATOR_HINTS = (
    "latest news",
    "breaking city news",
    "latest & breaking",
    "today's bangalore news",
    "bengaluru news:",
    "bangalore news:",
    "section/cities",
)


class WebSearchTool(NovaTool):
    name = "web_search"
    description = (
        "Search the web for current information. For city/local news, pass place and "
        "optional category (politics|traffic|business|sports|crime|tech|weather|general). "
        "Bare news queries cover a spectrum of those topics. Speak the returned speak "
        "field verbatim — do not read URLs or invent headlines."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "num_results": {
                "type": "integer",
                "description": "How many results to return (1-8). Defaults to 5.",
            },
            "place": {
                "type": "string",
                "description": "City/region for local news, e.g. Bangalore.",
            },
            "category": {
                "type": "string",
                "enum": [
                    "politics",
                    "traffic",
                    "business",
                    "sports",
                    "crime",
                    "tech",
                    "weather",
                    "general",
                ],
                "description": "News topic category. Omit for a multi-topic spectrum.",
            },
        },
        "required": ["query"],
    }

    def __init__(
        self,
        brave_api_key: str | None = _UNSET,
        serper_api_key: str | None = _UNSET,
        timeout: float = 5.0,
    ):
        self._brave_key = brave_api_key if brave_api_key is not _UNSET else get_api_key("BRAVE_API_KEY")
        self._serper_key = serper_api_key if serper_api_key is not _UNSET else get_api_key("SERPER_API_KEY")
        self._timeout = timeout

    def _brave_search(self, query: str, num_results: int) -> list[dict[str, str]]:
        if not self._brave_key:
            raise RuntimeError("BRAVE_API_KEY not configured")
        resp = httpx.get(
            BRAVE_URL,
            params={"q": query, "count": num_results},
            headers={"Accept": "application/json", "X-Subscription-Token": self._brave_key},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        results = resp.json()["web"]["results"]
        return [
            _compact_hit(r.get("title", ""), r.get("url", ""), r.get("description", ""))
            for r in results[:num_results]
        ]

    def _serper_search(self, query: str, num_results: int) -> list[dict[str, str]]:
        if not self._serper_key:
            raise RuntimeError("SERPER_API_KEY not configured")
        resp = httpx.post(
            SERPER_URL,
            json={"q": query, "num": num_results},
            headers={"X-API-KEY": self._serper_key, "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        results = resp.json().get("organic", [])
        return [
            _compact_hit(r.get("title", ""), r.get("link", ""), r.get("snippet", ""))
            for r in results[:num_results]
        ]

    def _brave_news(self, query: str, num_results: int) -> list[dict[str, str]]:
        if not self._brave_key:
            raise RuntimeError("BRAVE_API_KEY not configured")
        resp = httpx.get(
            BRAVE_NEWS_URL,
            params={"q": query, "count": num_results},
            headers={"Accept": "application/json", "X-Subscription-Token": self._brave_key},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        return [
            _compact_hit(r.get("title", ""), r.get("url", ""), r.get("description", ""))
            for r in results[:num_results]
        ]

    def _serper_news(self, query: str, num_results: int) -> list[dict[str, str]]:
        if not self._serper_key:
            raise RuntimeError("SERPER_API_KEY not configured")
        resp = httpx.post(
            SERPER_NEWS_URL,
            json={"q": query, "num": num_results},
            headers={"X-API-KEY": self._serper_key, "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        results = resp.json().get("news") or []
        return [
            _compact_hit(r.get("title", ""), r.get("link", ""), r.get("snippet", ""))
            for r in results[:num_results]
        ]

    def _search_once(self, query: str, num_results: int, *, news: bool) -> tuple[str, list[dict[str, str]]]:
        providers: tuple[tuple[str, Any], ...]
        if news:
            providers = (
                ("brave_news", self._brave_news),
                ("serper_news", self._serper_news),
                ("brave", self._brave_search),
                ("serper", self._serper_search),
            )
        else:
            providers = (("brave", self._brave_search), ("serper", self._serper_search))
        last_err = "unreachable"
        for name, fn in providers:
            try:
                return name, fn(query, num_results)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)[:120]
                continue
        raise RuntimeError(last_err)

    def execute(
        self,
        query: str,
        num_results: int = 5,
        place: str = "",
        category: str = "",
    ) -> dict[str, Any]:
        q = (query or "").strip()
        if not q:
            return {"status": "unavailable", "reason": "empty query", "speak": "Need a search query."}

        cat = (category or "").strip().lower()
        if cat and cat not in CATEGORY_QUERY:
            cat = ""
        loc = (place or "").strip() or _infer_place(q)
        news_mode = bool(cat) or _looks_like_news(q)

        try:
            if news_mode and (not cat or cat == "general"):
                return self._news_spectrum(q, loc, max(3, min(int(num_results or 5), 6)))
            if news_mode and cat:
                return self._news_category(q, loc, cat, max(1, min(int(num_results or 5), 5)))
            n = max(1, min(int(num_results or 5), 8))
            provider, results = self._search_once(q, n, news=False)
            results = _dedupe_filter(results)[:n]
            speak = _speak_general(results, q)
            return {
                "status": "success",
                "provider": provider,
                "query": q,
                "results": results,
                "speak": speak,
            }
        except Exception:  # noqa: BLE001
            return {
                "status": "unavailable",
                "query": q,
                "reason": "web search is unavailable: no search provider is configured or reachable",
                "speak": "Web search is unavailable right now.",
            }

    def _news_category(self, query: str, place: str, category: str, n: int) -> dict[str, Any]:
        topic = CATEGORY_QUERY.get(category, category)
        q = " ".join(p for p in (place, topic, "news today") if p).strip() or query
        provider, hits = self._search_once(q, n + 2, news=True)
        hits = _dedupe_filter(hits)[:n]
        for h in hits:
            h["category"] = category
        speak = _speak_news(hits, place or "here", category)
        return {
            "status": "success",
            "provider": provider,
            "mode": "news",
            "category": category,
            "place": place,
            "query": q,
            "results": hits,
            "speak": speak,
        }

    def _news_spectrum(self, query: str, place: str, n: int) -> dict[str, Any]:
        """One headline per topic — covers a spectrum without dumping aggregators."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        loc = place or _infer_place(query) or ""
        cats = list(NEWS_SPECTRUM)[: max(n, 1)]

        def _one(cat: str) -> tuple[str, str, list[dict[str, str]]]:
            topic = CATEGORY_QUERY[cat]
            q = " ".join(p for p in (loc, topic, "news today") if p)
            try:
                provider, hits = self._search_once(q, 3, news=True)
                return cat, provider, hits
            except Exception:  # noqa: BLE001
                return cat, "none", []

        # Parallel fan-out — sequential 5× news HTTP was blocking the voice loop
        # (live: hallucinated web_search hung; email checks never answered).
        by_cat: dict[str, tuple[str, list[dict[str, str]]]] = {}
        with ThreadPoolExecutor(max_workers=min(5, len(cats))) as pool:
            futs = {pool.submit(_one, cat): cat for cat in cats}
            for fut in as_completed(futs):
                cat, provider, hits = fut.result()
                by_cat[cat] = (provider, hits)

        picked: list[dict[str, str]] = []
        provider_used = "none"
        for cat in cats:
            if len(picked) >= n:
                break
            provider, hits = by_cat.get(cat, ("none", []))
            if provider != "none":
                provider_used = provider
            for hit in _dedupe_filter(hits):
                if _is_aggregator(hit.get("title", "")):
                    continue
                if any(_title_overlap(hit["title"], p["title"]) for p in picked):
                    continue
                hit = {**hit, "category": cat}
                picked.append(hit)
                break
        if not picked:
            q = " ".join(p for p in (loc, "news headlines today") if p) or query
            provider_used, hits = self._search_once(q, n + 2, news=True)
            picked = [{**h, "category": "general"} for h in _dedupe_filter(hits)[:n]]

        speak = _speak_news(picked, loc or "here", None)
        return {
            "status": "success",
            "provider": provider_used,
            "mode": "news_spectrum",
            "place": loc,
            "query": query,
            "results": picked,
            "speak": speak,
        }


def _compact_hit(title: str, url: str, snippet: str) -> dict[str, str]:
    domain = ""
    try:
        domain = urlparse(url or "").netloc.removeprefix("www.")[:40]
    except Exception:  # noqa: BLE001
        domain = ""
    # Keep a short url field for older tests/callers; prefer domain-only to save context.
    return {
        "title": (title or "").strip()[:120],
        "url": domain,
        "snippet": re.sub(r"\s+", " ", (snippet or "")).strip()[:140],
        "source": domain,
    }


def _looks_like_news(query: str) -> bool:
    q = query.lower()
    return bool(re.search(r"\b(news|headline|headlines|breaking)\b", q))


def _infer_place(query: str) -> str:
    m = re.search(r"\bin\s+([A-Za-z][A-Za-z]*(?:\s+[A-Za-z]+)*)", query, re.I)
    if m:
        raw = m.group(1).strip().rstrip(".,?!")
        if raw.lower() in {"bangalore", "bengaluru"}:
            return "Bengaluru"
        return raw.title()
    for city in ("Bangalore", "Bengaluru", "Mumbai", "Delhi", "Chennai", "Hyderabad", "Pune"):
        if city.lower() in query.lower():
            return "Bengaluru" if city.lower() in {"bangalore", "bengaluru"} else city
    return ""


def _is_aggregator(title: str) -> bool:
    t = title.lower()
    return any(h in t for h in _AGGREGATOR_HINTS)


def _title_tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2}


def _title_overlap(a: str, b: str) -> bool:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    return inter / min(len(ta), len(tb)) >= 0.6


def _dedupe_filter(results: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in results:
        title = (r.get("title") or "").strip()
        if not title or _is_aggregator(title):
            continue
        if any(_title_overlap(title, x.get("title", "")) for x in out):
            continue
        out.append(r)
    return out


def _speak_general(results: list[dict[str, str]], query: str) -> str:
    if not results:
        return f"No web results for {query}."
    parts = [r["title"] for r in results[:4] if r.get("title")]
    return "Top results: " + "; ".join(parts) + "."


def _speak_news(results: list[dict[str, str]], place: str, category: str | None) -> str:
    if not results:
        label = f"{category} news" if category else "news"
        return f"I couldn't find clear {label} for {place}."
    bits: list[str] = []
    for r in results[:6]:
        title = (r.get("title") or "").strip().rstrip(".")
        cat = (r.get("category") or category or "").strip()
        if cat and cat != "general":
            bits.append(f"{cat}: {title}")
        else:
            bits.append(title)
    if category and category != "general":
        return f"{place} {category} news: " + "; ".join(bits) + "."
    return f"Today in {place}: " + "; ".join(bits) + "."
