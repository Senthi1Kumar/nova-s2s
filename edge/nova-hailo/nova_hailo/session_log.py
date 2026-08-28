"""Per-session STT/LLM/TTS pair logs for Pi debug (not a product feature).

Writes ``session_logs/<id>/turns.jsonl`` + ``session.json`` + ``terminal.txt``.
Enable with ``NOVA_SESSION_LOG=1`` (default on). ``NOVA_SESSION_LOG=0`` disables.
The ``session_logs/`` directory is gitignored — keep files on the Pi only.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from nova_hailo.config import ROOT

LOG_ROOT = ROOT / "session_logs"


def _enabled() -> bool:
    raw = (os.environ.get("NOVA_SESSION_LOG") or "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


class _Tee:
    def __init__(self, primary: TextIO, extra: TextIO):
        self.primary = primary
        self.extra = extra

    def write(self, data: str) -> int:
        n = self.primary.write(data)
        try:
            self.extra.write(data)
            self.extra.flush()
        except Exception:
            pass
        return n

    def flush(self) -> None:
        self.primary.flush()
        try:
            self.extra.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        return bool(getattr(self.primary, "isatty", lambda: False)())


class SessionLog:
    """One folder per process/session; thread-safe append of turn pairs."""

    def __init__(self, session_id: str, *, root: Path | None = None):
        self.session_id = session_id
        self.enabled = _enabled()
        self.dir = (root or LOG_ROOT) / session_id
        self._lock = threading.Lock()
        self._jsonl: TextIO | None = None
        self._term: TextIO | None = None
        self._old_out = None
        self._old_err = None
        self._n = 0
        if not self.enabled:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "session_id": session_id,
            "started": datetime.now(timezone.utc).isoformat(),
            "cwd": str(ROOT),
        }
        (self.dir / "session.json").write_text(json.dumps(meta, indent=2) + "\n")
        self._jsonl = open(self.dir / "turns.jsonl", "a", encoding="utf-8")
        self._term = open(self.dir / "terminal.txt", "a", encoding="utf-8")
        self._old_out, self._old_err = sys.stdout, sys.stderr
        sys.stdout = _Tee(self._old_out, self._term)  # type: ignore[assignment]
        sys.stderr = _Tee(self._old_err, self._term)  # type: ignore[assignment]
        print(f"[session_log] {self.dir}", flush=True)

    def log_turn(
        self,
        *,
        user: str = "",
        assistant: str = "",
        stt_engine: str | None = None,
        llm_backend: str | None = None,
        llm_model: str | None = None,
        tool_name: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        metrics: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled or self._jsonl is None:
            return
        self._n += 1
        rec: dict[str, Any] = {
            "n": self._n,
            "t": datetime.now(timezone.utc).isoformat(),
            "user": user or "",
            "assistant": assistant or "",
            "stt_engine": stt_engine,
            "llm_backend": llm_backend,
            "llm_model": llm_model,
            "tool_name": tool_name,
            "tools": tools or [],
            "metrics": metrics or {},
        }
        if extra:
            rec.update(extra)
        line = json.dumps(rec, default=str)
        with self._lock:
            self._jsonl.write(line + "\n")
            self._jsonl.flush()
            if self._term:
                self._term.write(
                    f"\n----- turn {self._n} -----\n"
                    f"YOU: {user}\n"
                    f"NOVA: {assistant}\n"
                    f"stt={stt_engine} llm={llm_backend}:{llm_model} tool={tool_name}\n"
                    f"metrics={json.dumps(metrics or {}, default=str)[:800]}\n"
                )
                self._term.flush()

    def close(self) -> None:
        if not self.enabled:
            return
        if self._old_out is not None:
            sys.stdout = self._old_out
        if self._old_err is not None:
            sys.stderr = self._old_err
        for fh in (self._jsonl, self._term):
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
        self._jsonl = self._term = None


_active: SessionLog | None = None
_active_lock = threading.Lock()


def start_process_log() -> SessionLog | None:
    global _active
    if not _enabled():
        return None
    with _active_lock:
        if _active is not None:
            return _active
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        sid = f"{stamp}_{os.getpid()}"
        _active = SessionLog(sid)
        return _active


def current() -> SessionLog | None:
    return _active


def stop_process_log() -> None:
    global _active
    with _active_lock:
        if _active is not None:
            _active.close()
            _active = None
