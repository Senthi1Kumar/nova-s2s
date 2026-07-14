"""Real temp-SQLite tests for memory tools (no mocking)."""
from __future__ import annotations

from nova.tools.memory import MemoryDB, RecallMemoriesTool, RememberTool


def test_remember_then_recall_roundtrip(tmp_path):
    db = MemoryDB(tmp_path / "memory.db")
    out = RememberTool(db).execute(text="Alex prefers 20C in the cabin")
    assert out["status"] == "success"
    recalled = RecallMemoriesTool(db).execute()
    assert recalled["status"] == "success"
    assert any("20C" in m["text"] for m in recalled["memories"])


def test_recall_returns_most_recent_first_capped(tmp_path):
    db = MemoryDB(tmp_path / "memory.db")
    for i in range(15):
        db.add(f"note {i}")
    memories = RecallMemoriesTool(db).execute()["memories"]
    assert len(memories) == 10
    assert memories[0]["text"] == "note 14"


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "memory.db"
    MemoryDB(path).add("persisted")
    assert any(m["text"] == "persisted" for m in MemoryDB(path).recent())


def test_schemas_well_formed(tmp_path):
    db = MemoryDB(tmp_path / "m.db")
    for tool in (RememberTool(db), RecallMemoriesTool(db)):
        ft = tool.to_function_tool()
        assert ft["type"] == "function" and ft["name"] and ft["parameters"]["type"] == "object"
