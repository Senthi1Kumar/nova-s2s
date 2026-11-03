"""Streaming text segmentation for clause-level TTS."""

from __future__ import annotations

import re


class SentenceBuffer:
    def __init__(self) -> None:
        self._text = ""

    def feed(self, token: str) -> list[str]:
        self._text += token
        sentences: list[str] = []
        while True:
            clause = re.search(r"[.!?;,]\s+", self._text)
            words = list(re.finditer(r"\S+\s+", self._text))
            end = clause.end() if clause else None
            if len(words) >= 8 and (end is None or words[7].end() < end):
                end = words[7].end()
            if end is None:
                break
            sentence, self._text = self._text[:end].strip(), self._text[end:]
            if sentence and re.search(r"\w", sentence):
                sentences.append(sentence)
        return sentences

    def flush(self) -> str:
        sentence, self._text = self._text.strip(), ""
        return sentence if re.search(r"\w", sentence) else ""
