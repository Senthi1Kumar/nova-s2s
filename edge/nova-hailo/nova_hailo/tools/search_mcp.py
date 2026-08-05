"""FastMCP + in-process web search / deep research tools for nova-hailo.

Callers: OemToolGateway (deep_research), pipeline poll_research_job, optional stdio MCP.
Prefer in-process function calls on the Pi (no extra network hop). FastMCP registration
is optional when the `mcp` package is installed.
"""
from __future__ import annotations

import os
import re
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
    SearchResultCleaner,
    is_numeric_fact_query,
    literal_number,
)

TAVILY_URL = "https://api.tavily.com/search"
RESEARCH_FILLER = "Looking that up."
DEFAULT_SEARCH_TIMEOUT = 1.5
DEFAULT_TAVILY_TIMEOUT = 12.0


def _compact(title: Any, url: Any, snippet: Any) -> dict[str, str]:
    domain = ""
    try:
        domain = urlparse(str(url or "")).netloc.removeprefix("www.")[:40]
    except Exception:  # noqa: BLE001
        domain = ""
    return {
        "title": str(title or "").strip()[:120],
        "url": domain,
        "snippet": re.sub(r"\s+", " ", str(snippet or "")).strip()[:180],
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
        r"(?i)\b(search( the web)?( for)?|look\s*up|google|research|dig\s+into|"
        r"deep\s+dive|look\s+into|investigate)\b",
        " ",
        q,
    )
    return re.sub(r"\s+", " ", q).strip() or (query or "").strip()


def brave_search(
    query: str, api_key: str, timeout_sec: float = DEFAULT_SEARCH_TIMEOUT
) -> list[dict[str, str]]:
    resp = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": 5, "search_lang": "en"},
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


def answer_from_hits(query: str, hits: list, *, tool_name: str = "web_search") -> dict[str, Any]:
    cleaner = SearchResultCleaner()
    evidence = cleaner.evidence_block(hits)
    if not evidence:
        return _unavailable(tool_name, "no_usable_snippets")
    if is_numeric_fact_query(query):
        num = literal_number(evidence)
        speak = (
            f"Search results say {num}."
            if num
            else "I couldn't find a reliable number for that."
        )
        return _ok(
            tool_name,
            speak,
            {"evidence": evidence, "numeric": True, "needs_summary": False},
        )
    return _ok(
        tool_name,
        cleaner.speak_fallback(hits),
        {"evidence": evidence, "numeric": False, "needs_summary": True},
    )


def web_search(
    query: str,
    *,
    timeout_sec: float = DEFAULT_SEARCH_TIMEOUT,
    serper_fallback: bool = True,
) -> dict[str, Any]:
    """Sync Brave → Serper lookup; fail-closed without keys."""
    q = _normalize_query(query)
    if not q:
        return _unavailable("web_search", "empty_query")
    brave = (os.environ.get("BRAVE_API_KEY") or "").strip()
    serper = (os.environ.get("SERPER_API_KEY") or "").strip()
    if brave:
        try:
            hits = brave_search(q, brave, timeout_sec=timeout_sec)
            if hits:
                return answer_from_hits(q, hits)
        except Exception as exc:  # noqa: BLE001
            if not serper_fallback or not serper:
                return _unavailable("web_search", f"brave_failed:{exc}")
    if serper_fallback and serper:
        try:
            hits = serper_search(q, serper, timeout_sec=timeout_sec)
            if hits:
                return answer_from_hits(q, hits)
        except Exception as exc:  # noqa: BLE001
            return _unavailable("web_search", f"serper_failed:{exc}")
    return _unavailable("web_search", "no_search_api_key")


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
        speak = re.sub(r"\s+", " ", answer).strip()
        if len(speak) > 280:
            speak = speak[:277].rstrip() + "…"
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
    "deep_research",
    "research_status",
    "build_fastmcp_app",
    "answer_from_hits",
]
