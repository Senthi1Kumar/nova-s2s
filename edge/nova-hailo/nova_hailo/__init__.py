"""Nova-Hailo edge voice agent package.

Loads `.env` at package import so every entrypoint (web app, CLI, matrix runner,
tools) sees BRAVE_API_KEY / GOOGLE_OAUTH_* in os.environ. Putting this in
config.py was not enough: nova_hailo.tools.oem_tools reads the keys at
construction and does not import config, so the tools silently reported
"no_search_api_key" / "oauth_not_configured" — measured 2026-07-29.
"""
from pathlib import Path

try:  # pragma: no cover - trivial bootstrap
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except Exception:
    pass
