"""Ensure repo root is importable when pytest is pointed at eval/tests.

Callers: pytest loads this automatically for eval/tests.
API: mutates sys.path only. No data files.
User: Verify `uv run pytest -q eval/tests`.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
