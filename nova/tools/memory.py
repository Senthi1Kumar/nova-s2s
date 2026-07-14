"""Session memory: explicit remember/recall tools over a small SQLite store.

Deliberately explicit (the model calls ``remember``) rather than automatic
extraction — CLAUDE.md failure mode #2: memory-extraction LLM calls contending
with the single chat engine mid-turn corrupt the TTS stream. Tool-call-driven
writes ride the normal turn path and cost nothing extra.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from nova.tools.base import NovaTool

DEFAULT_MEMORY_PATH = Path(__file__).resolve().parent.parent.parent / "runtime" / "memory.db"


class MemoryDB:
    def __init__(self, db_path: str | Path = DEFAULT_MEMORY_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS memories("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "text TEXT NOT NULL, created_at TEXT NOT NULL)"
            )

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def add(self, text: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO memories(text, created_at) VALUES (?, ?)",
                (text, time.strftime("%Y-%m-%dT%H:%M:%S")),
            )
            return int(cur.lastrowid)

    def recent(self, n: int = 10) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, text, created_at FROM memories ORDER BY id DESC LIMIT ?", (n,)
            )
            return [dict(r) for r in rows]


class RememberTool(NovaTool):
    name = "remember"
    description = (
        "Save a fact or preference the driver wants remembered across drives "
        "(e.g. 'remember I prefer 20 degrees'). Keep it one short sentence."
    )
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "The fact to remember."}},
        "required": ["text"],
    }

    def __init__(self, db: MemoryDB):
        self._db = db

    def execute(self, text: str) -> dict[str, Any]:
        memory_id = self._db.add(text)
        return {"status": "success", "saved": text, "memory_id": memory_id}


class RecallMemoriesTool(NovaTool):
    name = "recall_memories"
    description = "List the driver's saved memories/preferences, most recent first."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, db: MemoryDB):
        self._db = db

    def execute(self) -> dict[str, Any]:
        return {"status": "success", "memories": self._db.recent()}
