"""Shared ``.env`` loading for external tool modules.

Introduces ``python-dotenv`` as a new pattern for nova_v3 (not used elsewhere
in the repo yet) so ``websearch.py`` / ``research.py`` can find real API keys
(``BRAVE_API_KEY``, ``SERPER_API_KEY``, ``TAVILY_API_KEY``) from a local
``.env`` without the app's config loading changing. ``load_dotenv()`` is
idempotent and safe to call from every module that needs a key; it never
overwrites variables already set in the real environment.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def get_api_key(name: str) -> str | None:
    """Return the named env var, or ``None`` if unset/blank."""
    value = os.getenv(name)
    return value if value else None
