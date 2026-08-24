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
# Page chrome that Exa raw `text` often leads with (dates, bylines, "min read").
_LEAD_CHROME_RE = re.compile(
    r"^(?:"
    r"(?:published|updated|posted|written|byline)\b[^.]{0,48}[.—-]\s*|"
    r"(?:by\s+[A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,3}\s*[.—-]\s*)|"
    r"(?:\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s*[.—-]\s*)|"
    r"(?:\d+\s+min(?:ute)?s?\s+read\s*[.—-]?\s*)|"
    r"(?:skip to (?:main )?content\s*[.—-]?\s*)"
    r")+",
    re.I,
)
_CHROME_ONLY_RE = re.compile(
    r"^(published|updated|posted|skip to|min read|share this|"
    r"subscribe|advertisement|photo:|image:)\b",
    re.I,
)
# Homepage / section SEO — not a spoken news answer.
_BOILERPLATE_RE = re.compile(
    r"("
    r"source for (breaking )?news|"
    r"delivers the latest|"
    r"latest (news|updates) and top stories|"
    r"browse \d+ of the top|"
    r"find the latest .{0,40} news|"
    r"covering .{0,40} and all of the greater|"
    r"including ai chatbots|"
    r"ethical issues ai raises"
    r")",
    re.I,
)

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
        parts: list[str] = []
        used = 0
        budget = 320
        for snip in snips:
            text = _LEAD_CHROME_RE.sub("", snip.text).strip(" -|–—,")
            if _BOILERPLATE_RE.search(text) or _BOILERPLATE_RE.search(snip.title):
                # Prefer a concrete headline over "your source for news…".
                title = (snip.title or "").strip()
                if title and not _BOILERPLATE_RE.search(title) and len(title) >= 20:
                    text = title if title.endswith((".", "!", "?")) else title + "."
                else:
                    continue
            if not text or len(text) < MIN_SNIPPET_CHARS:
                continue
            if _CHROME_ONLY_RE.search(text) and len(text) < 80:
                continue
            chunk = text
            if used + len(chunk) > budget:
                remaining = budget - used
                if remaining < 60:
                    break
                chunk = chunk[:remaining].rsplit(" ", 1)[0].rstrip(" ,;:-") + "."
            parts.append(chunk)
            used += len(chunk) + 1
            if used >= 160 or len(parts) >= 2:
                break
        if not parts:
            first = snips[0].text
            if len(first) > 220:
                first = first[:219].rsplit(" ", 1)[0].rstrip(" ,;:-") + "."
            return first
        return " ".join(parts)


# Numeric asks must never reach the LLM. Measured 2026-07-30: given evidence
# containing no price at all, Qwen2-1.5B answered "Tesla stock price is currently
# $304.18" despite an explicit "use ONLY these facts" instruction. Snippets rarely
# carry live figures, so the model fills the gap. Speak a literal quote or decline.
_NUMERIC_Q_RE = re.compile(
    r"\b(price|quote|stock|share|worth|cost|how\s+much|score|temperature|"
    r"how\s+(hot|cold|far|many|long))\b",
    re.I,
)
# Allow uncomma'd large figures (Bitcoin ~$95000) and standard $1,234.56 forms.
_MONEY_BODY = r"\d{1,7}(?:,\d{3})*(?:\.\d+)?"
_LITERAL_NUM_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    rf"[$€£]\s?{_MONEY_BODY}"
    rf"|\d{{1,7}}(?:,\d{{3}})*(?:\.\d+)?\s?(?:USD|EUR|GBP|percent|%|°C|°F|km|miles|mph)"
    r")",
    re.I,
)


def is_numeric_fact_query(query: str) -> bool:
    """True when the answer is a number that snippets probably do not contain."""
    return bool(_NUMERIC_Q_RE.search(query or ""))


_CURRENCY_NUM_RE = re.compile(
    rf"(?<![A-Za-z0-9])([$€£]\s?{_MONEY_BODY})",
    re.I,
)
_STOCK_Q_RE = re.compile(
    r"\b(stock|share\s+price|ticker|nasdaq|nyse|bitcoin|btc|crypto)\b",
    re.I,
)
# Left context that marks a *change* amount, not the share price.
_PRICE_CHANGE_CTX_RE = re.compile(
    r"(?:\b(?:up|down|rose|fell|gain(?:ed|s)?|drop(?:ped)?|change[ds]?|moved|"
    r"increased|decreased|surged|slid|tumbled|climbed)\b|"
    r"[+\-±]|by)\s*$",
    re.I,
)
# Right/left context that marks a real share/quote price.
_PRICE_QUOTE_CTX_RE = re.compile(
    r"(?:price|quote|trading\s+at|trades?\s+at|last|close[sd]?|opens?|"
    r"share|stock|at)\s*$",
    re.I,
)


def _parse_money(raw: str) -> float | None:
    try:
        return float(re.sub(r"[^\d.]", "", raw.replace(",", "")))
    except ValueError:
        return None


def _near_keyword(body: str, pos: int, keywords: Iterable[str], radius: int = 72) -> bool:
    """True if any keyword appears within radius chars of pos."""
    lo = max(0, pos - radius)
    hi = min(len(body), pos + radius)
    window = body[lo:hi]
    for kw in keywords:
        k = (kw or "").strip()
        if not k:
            continue
        if re.search(rf"\b{re.escape(k)}\b", window, re.I):
            return True
    return False


def pick_stock_price(
    text: str,
    *,
    near: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
) -> str | None:
    """Pick the most plausible *share price* from evidence, not a daily change.

    Live 2026-08-08: Amazon stock answer spoke "$2.2" (a change / fragment)
    instead of the ~$200+ share price. Score every currency amount and take
    the best; decline when nothing looks like a quote.

    Optional ``near`` keywords: when provided, only amounts within the
    proximity window of an entity/ticker count (multi-ticker honesty — do not
    invent a second quote from unrelated $ amounts). Also boosts score.
    Optional ``exclude`` skips already-chosen prices.
    """
    body = text or ""
    excluded = {re.sub(r"\s+", " ", e).strip() for e in (exclude or []) if e}
    near_kws = [k for k in (near or []) if k]
    best: tuple[float, str] | None = None
    for m in _CURRENCY_NUM_RE.finditer(body):
        raw = re.sub(r"\s+", " ", m.group(1)).strip()
        if raw in excluded:
            continue
        # Multi-entity: require proximity so oil $72 is never "Tesla".
        if near_kws and not _near_keyword(body, m.start(), near_kws):
            continue
        val = _parse_money(raw)
        if val is None:
            continue
        left = body[max(0, m.start() - 28) : m.start()]
        score = 0.0
        if _PRICE_CHANGE_CTX_RE.search(left):
            score -= 80.0
        if _PRICE_QUOTE_CTX_RE.search(left):
            score += 40.0
        # Typical equity range; high band covers Bitcoin / crypto quotes.
        if 15.0 <= val <= 8000.0:
            score += 35.0
        elif 8000.0 < val <= 250000.0:
            score += 28.0
        elif 5.0 <= val < 15.0:
            score += 5.0
        else:
            score -= 25.0  # $2.2, $0.97, extreme outliers
        if re.search(r"\.\d{2}\b", raw):
            score += 12.0
        if near_kws:
            score += 55.0
        # Prefer earlier (higher-ranked) hits slightly.
        score -= m.start() / max(len(body), 1) * 5.0
        if best is None or score > best[0]:
            best = (score, raw)
    if best is None or best[0] < 10.0:
        return None
    return best[1]


def literal_number(text: str, *, query: str | None = None) -> str | None:
    """Return a number that literally appears in the evidence, else None.

    For stock/share/crypto queries use pick_stock_price (quote vs change).
    Otherwise take the first currency/unit match on the top result line only.
    """
    first_line = (text or "").split("\n", 1)[0]
    if query and _STOCK_Q_RE.search(query):
        return pick_stock_price(text or "")
    m = _LITERAL_NUM_RE.search(first_line)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None
