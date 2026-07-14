"""Sentence-boundary chunking of streamed LLM text for per-sentence TTS."""

from __future__ import annotations

import re

_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U0001f1e0-\U0001f1ff"
    "\U00002600-\U000027bf"
    "\U0000fe00-\U0000fe0f"
    "\U0000200d"
    "\U00002190-\U000021ff"
    "\U00002b00-\U00002bff"
    "]+",
    re.UNICODE,
)
_MARKDOWN_RE = re.compile(r"[*_`#>]+")
_WS_RE = re.compile(r"\s+")

# End-of-sentence punctuation followed by whitespace or end-of-buffer. Decimal
# points and single-capital initials are rejected in _find_boundary.
_BOUNDARY_RE = re.compile(r"[.!?:;\n](?=\s|$)")


def clean_for_speech(text: str) -> str:
    text = _EMOJI_RE.sub("", text)
    text = _MARKDOWN_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()


class SentenceChunker:
    # Default kept comfortably under Kokoro's ~126-phoneme hard budget
    # (nova/engine/tts_g2p.py) at the ~1.05-1.1 phoneme/char ratio measured
    # sentence boundaries for streaming TTS --
    # 200 chars was unsafe (maps to ~210-220 phonemes) and could raise
    # ValueError deep in StreamingSynthesizer on ordinary long, lightly
    # punctuated LLM output.
    def __init__(self, max_chars: int = 100):
        self._buffer = ""
        self.max_chars = max_chars

    def _find_boundary(self) -> int | None:
        for match in _BOUNDARY_RE.finditer(self._buffer):
            i = match.start()
            before = self._buffer[:i]
            if self._buffer[i] == ".":
                if (
                    i > 0
                    and before[-1].isdigit()
                    and i + 1 < len(self._buffer)
                    and self._buffer[i + 1].isdigit()
                ):
                    continue
                tail = before.rsplit(" ", 1)[-1]
                if len(tail) == 1 and tail.isupper():
                    continue
            return i
        return None

    def _force_split_point(self) -> int:
        window = self._buffer[: self.max_chars]
        for sep in (",", " "):
            k = window.rfind(sep)
            if k > 0:
                return k
        return self.max_chars

    def push(self, delta: str) -> list[str]:
        self._buffer += delta
        out: list[str] = []
        while True:
            i = self._find_boundary()
            if i is not None:
                chunk, self._buffer = self._buffer[: i + 1], self._buffer[i + 1 :].lstrip()
            elif len(self._buffer) > self.max_chars:
                k = self._force_split_point()
                chunk, self._buffer = self._buffer[: k + 1], self._buffer[k + 1 :].lstrip()
            else:
                break
            cleaned = clean_for_speech(chunk)
            if cleaned:
                out.append(cleaned)
        return out

    def flush(self) -> str | None:
        cleaned = clean_for_speech(self._buffer)
        self._buffer = ""
        return cleaned or None
