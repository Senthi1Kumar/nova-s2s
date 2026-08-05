"""Sentence / clause chunking for streaming TTS (hailo-apps pattern)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SentenceBuffer:
    """Accumulate LLM tokens and emit speakable clauses early for TTFA."""

    first_chunk_min_chars: int = 15
    voice_one_sentence: bool = True
    buffer: str = ""
    first_chunk_sent: bool = False
    sentences_emitted: int = 0
    stop_generation: bool = False
    emitted: list[str] = field(default_factory=list)

    def feed(self, chunk: str) -> list[str]:
        """Return list of clauses ready to speak; update internal buffer."""
        if not chunk or self.stop_generation:
            return []
        self.buffer += chunk
        out: list[str] = []

        if self.voice_one_sentence and self.sentences_emitted >= 1:
            # Keep absorbing until caller stops LLM; do not emit more.
            return []

        is_first = not self.first_chunk_sent
        if is_first:
            delimiters = [".", "?", "!", ",", ":", ";", "-"]
            min_force = self.first_chunk_min_chars
        else:
            delimiters = [".", "?", "!"]
            min_force = 0

        while True:
            positions = {self.buffer.find(d): d for d in delimiters if self.buffer.find(d) != -1}
            if is_first and not positions and len(self.buffer) >= min_force:
                last_space = self.buffer.rfind(" ")
                if last_space > 5:
                    positions[last_space] = " "

            if not positions:
                break

            first_pos = min(positions.keys())
            piece = self.buffer[: first_pos + 1].strip()
            self.buffer = self.buffer[first_pos + 1 :]
            if piece:
                out.append(piece)
                self.emitted.append(piece)
                self.sentences_emitted += 1
                self.first_chunk_sent = True
                is_first = False
                delimiters = [".", "?", "!"]
                if self.voice_one_sentence and self.sentences_emitted >= 1:
                    self.stop_generation = True
                    break
        return out

    def flush(self) -> list[str]:
        if self.voice_one_sentence and self.sentences_emitted >= 1:
            self.buffer = ""
            return []
        text = self.buffer.strip()
        self.buffer = ""
        if text:
            self.emitted.append(text)
            self.sentences_emitted += 1
            self.first_chunk_sent = True
            return [text]
        return []

    @property
    def full_text(self) -> str:
        return " ".join(self.emitted).strip()
