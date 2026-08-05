"""Post-process Brave/Serper results into a small, quotable evidence block.

Raw Brave payloads measured 2026-07-30 run 12.5-90 KB (~3.1K-22.4K tokens) — up
to 11x the compiled 2048-token context. Almost all of it is metadata
(`meta_url`, `profile`, `page_age`, `thumbnail`, `family_friendly`, ...) plus
side blocks (`infobox`, `faq`, `mixed`, `videos`, `news`). Only
`web.results[].title` and `.description` carry answer content, and those total
~240-360 tokens for five results.

Descriptions arrive with `<strong>` highlight markup and `...` truncation
ellipses; both reach the model verbatim unless stripped.

Source URLs are deliberately dropped: a 1.5B reads them as text to recite, which
is how "Top results: ... | AccuWeather" ended up spoken aloud.
"""
from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")
_ELLIPSIS_RE = re.compile(r"\s*\.\.\.\s*")
_WS_RE = re.compile(r"\s+")
# Domains and site names inside description text get recited aloud
# ("available at The Weather Channel and Weather.com") — strip them too.
_DOMAIN_RE = re.compile(r"\b[\w.-]+\.(com|org|net|io|ai|co\.uk|de|co)\b", re.I)
_SITE_NAME_RE = re.compile(
    r"\b(the weather channel|accuweather|wikipedia|yahoo finance|cnbc|reuters|"
    r"bloomberg|forbes|techcrunch|the verge)\b", re.I)
# Site names trail titles as "Real Title - Wikipedia" / "... | AccuWeather".
_TITLE_SITE_RE = re.compile(r"\s*[|–—-]\s*[^|–—-]{2,30}$")

DEFAULT_MAX_CHARS = 700
DEFAULT_MAX_RESULTS = 4
MIN_SNIPPET_CHARS = 40


def _clean_text(raw: str) -> str:
    t = html.unescape(str(raw or ""))
    t = _TAG_RE.sub(" ", t)
    t = _ELLIPSIS_RE.sub(" ", t)
    t = _DOMAIN_RE.sub(" ", t)
    t = _SITE_NAME_RE.sub(" ", t)
    # Removing a site name can strand its connectives ("... radar from and").
    t = re.sub(r"\s+(from|at|on|via|by|and|with)(\s+(and|from|at|on|the))*\s*(?=[.,;]|$)", "", t, flags=re.I)
    t = re.sub(r"\s*[,;]\s*(?=[.,;]|$)", "", t)
    return _WS_RE.sub(" ", t).strip(" -|–—,")


def _clean_title(raw: str) -> str:
    t = _clean_text(raw)
    stripped = _TITLE_SITE_RE.sub("", t).strip()
    return stripped if len(stripped) >= 10 else t


@dataclass(frozen=True)
class Snippet:
    title: str
    text: str

    def as_line(self) -> str:
        return f"{self.title}: {self.text}" if self.title else self.text


class SearchResultCleaner:
    """Turn raw provider hits into a bounded, markup-free evidence block."""

    def __init__(
        self,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_results: int = DEFAULT_MAX_RESULTS,
    ):
        self.max_chars = int(max_chars)
        self.max_results = int(max_results)

    def snippets(self, hits: Iterable[dict[str, Any]]) -> list[Snippet]:
        out: list[Snippet] = []
        seen: set[str] = set()
        for hit in hits or []:
            text = _clean_text(hit.get("description") or hit.get("snippet") or "")
            if len(text) < MIN_SNIPPET_CHARS:
                continue
            key = text[:60].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(Snippet(title=_clean_title(hit.get("title") or ""), text=text))
            if len(out) >= self.max_results:
                break
        return out

    def evidence_block(self, hits: Iterable[dict[str, Any]]) -> str:
        """Numbered, budget-capped text for the LLM prompt. Empty if nothing usable."""
        lines: list[str] = []
        used = 0
        for i, snip in enumerate(self.snippets(hits), start=1):
            line = f"{i}. {snip.as_line()}"
            if used + len(line) > self.max_chars:
                remaining = self.max_chars - used
                if remaining < MIN_SNIPPET_CHARS:
                    break
                line = line[:remaining].rsplit(" ", 1)[0]
            lines.append(line)
            used += len(line) + 1
            if used >= self.max_chars:
                break
        return "\n".join(lines)

    def speak_fallback(self, hits: Iterable[dict[str, Any]]) -> str:
        """Deterministic spoken answer when the LLM must not be trusted to phrase it."""
        snips = self.snippets(hits)
        if not snips:
            return "I couldn't find anything useful on that."
        first = snips[0].text
        if len(first) > 220:
            first = first[:219].rsplit(" ", 1)[0].rstrip(" ,;:-") + "."
        return first


# Numeric asks must never reach the LLM. Measured 2026-07-30: given evidence
# containing no price at all, Qwen2-1.5B answered "Tesla stock price is currently
# $304.18" despite an explicit "use ONLY these facts" instruction. Snippets rarely
# carry live figures, so the model fills the gap. Speak a literal quote or decline.
_NUMERIC_Q_RE = re.compile(
    r"\b(price|quote|stock|share|worth|cost|how\s+much|score|temperature|"
    r"how\s+(hot|cold|far|many|long))\b",
    re.I,
)
_LITERAL_NUM_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    r"[$€£]\s?\d{1,3}(?:,\d{3})*(?:\.\d+)?"
    r"|\d{1,3}(?:,\d{3})*(?:\.\d+)?\s?(?:USD|EUR|GBP|percent|%|°C|°F|km|miles|mph)"
    r")",
    re.I,
)


def is_numeric_fact_query(query: str) -> bool:
    """True when the answer is a number that snippets probably do not contain."""
    return bool(_NUMERIC_Q_RE.search(query or ""))


def literal_number(text: str) -> str | None:
    """Return a number that literally appears in the evidence, else None."""
    m = _LITERAL_NUM_RE.search(text or "")
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None
