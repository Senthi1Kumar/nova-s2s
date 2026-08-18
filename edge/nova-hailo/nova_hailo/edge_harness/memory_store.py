"""Durable compact memories for startup greet — never full transcripts."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

DEFAULT_MEMORY_PATH = Path("runtime/nova_memory.json")
MAX_FACTS = 12
STARTUP_GREET_PREFIX = "Hey — um, last time we talked about"


def default_memory_path() -> Path:
    return DEFAULT_MEMORY_PATH


class MemoryStore:
    """JSON file of short facts. Cross-session; host-owned."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_MEMORY_PATH

    def load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def save(self, facts: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(facts[-MAX_FACTS:], indent=2), encoding="utf-8")

    def add(self, text: str, *, kind: str = "fact") -> None:
        text = (text or "").strip()
        if not text:
            return
        facts = self.load()
        facts.append({"text": text[:200], "kind": kind, "ts": time.time()})
        self.save(facts)

    def last_session_fact(self) -> str | None:
        facts = self.load()
        for item in reversed(facts):
            t = str(item.get("text") or "").strip()
            if t:
                return t
        return None

    def startup_greet(self) -> str:
        fact = self.last_session_fact()
        if not fact:
            return "Hey — I'm Nova. What can I do for you?"
        # Compact: one clause, no full transcript dump.
        snippet = fact.split(".")[0].strip()
        if len(snippet) > 80:
            snippet = snippet[:77].rsplit(" ", 1)[0] + "…"
        return f"{STARTUP_GREET_PREFIX} {snippet}."
