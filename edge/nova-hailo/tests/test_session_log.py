from __future__ import annotations

import json
from pathlib import Path

from nova_hailo.session_log import SessionLog


def test_session_log_writes_jsonl_and_txt(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVA_SESSION_LOG", "1")
    log = SessionLog("t_unit", root=tmp_path)
    try:
        print("hello terminal")
        log.log_turn(
            user="what is hailo",
            assistant="Hailo is an NPU.",
            stt_engine="parakeet",
            llm_backend="openrouter",
            llm_model="deepseek/deepseek-v4-flash-0731",
            tool_name="web_search",
            metrics={"stt_ms": 120, "ttfa_ms": 800},
        )
    finally:
        log.close()
    d = tmp_path / "t_unit"
    lines = (d / "turns.jsonl").read_text().strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["user"] == "what is hailo"
    assert rec["assistant"].startswith("Hailo")
    assert rec["stt_engine"] == "parakeet"
    assert rec["tool_name"] == "web_search"
    term = (d / "terminal.txt").read_text()
    assert "hello terminal" in term
    assert "YOU: what is hailo" in term
