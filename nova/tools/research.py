"""Deeper, multi-source research answer, exposed as a ``NovaTool``.

Uses Tavily's ``/search`` endpoint (confirmed against
https://docs.tavily.com/documentation/api-reference/endpoint/search.md,
2026-07-09) with ``search_depth="advanced"`` and ``include_answer="advanced"``
— Tavily's recommended settings for a cited, synthesized answer rather than a
quick single-snippet lookup. This is deliberately NOT Tavily's async
``/research`` job endpoint (create task -> poll for completion): that's built
for long-running report generation and would blow the voice loop's latency
budget. The synchronous advanced-search endpoint gives a same-round-trip
multi-result answer, which is what "research X" needs here.

Request: ``POST https://api.tavily.com/search`` with ``Authorization: Bearer
<TAVILY_API_KEY>`` and a JSON body ``{"query", "search_depth", "max_results",
"include_answer", "chunks_per_source"}``.
Response: ``{"query", "answer", "results": [{"title","url","content","score"}],
"response_time"}``.
"""
from __future__ import annotations

from typing import Any

import httpx

from nova.tools._env import get_api_key
from nova.tools.base import NovaTool

TAVILY_URL = "https://api.tavily.com/search"

_UNSET: Any = object()


class ResearchTool(NovaTool):
    name = "research_topic"
    description = (
        "Do deeper, multi-source research on a topic or question and return a synthesized answer "
        "plus supporting sources. Use for 'research X' / 'look into X' requests, not quick lookups."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The topic or question to research."},
            "max_results": {
                "type": "integer",
                "description": "How many source results to include (1-10). Defaults to 5.",
            },
        },
        "required": ["query"],
    }

    def __init__(self, tavily_api_key: str | None = _UNSET, timeout: float = 10.0):
        self._api_key = tavily_api_key if tavily_api_key is not _UNSET else get_api_key("TAVILY_API_KEY")
        self._timeout = timeout

    def execute(self, query: str, max_results: int = 5) -> dict[str, Any]:
        if not self._api_key:
            return {
                "status": "unavailable",
                "query": query,
                "reason": "research is unavailable: TAVILY_API_KEY is not configured",
            }

        try:
            resp = httpx.post(
                TAVILY_URL,
                json={
                    "query": query,
                    "search_depth": "advanced",
                    "chunks_per_source": 3,
                    "max_results": max_results,
                    "include_answer": "advanced",
                },
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"status": "unavailable", "query": query, "reason": f"research failed: {exc}"}

        sources = [
            {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
            for r in data.get("results", [])
        ]
        return {
            "status": "success",
            "query": query,
            "answer": data.get("answer", ""),
            "sources": sources,
        }
