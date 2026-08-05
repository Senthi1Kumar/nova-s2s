#!/usr/bin/env python3
"""One-shot Google Workspace OAuth login for Nova-Hailo (CLI).

Self-contained — does NOT import nova-s2s scripts.
Writes runtime/google_oauth/tokens.json (0600).

Prereq — Web OAuth client redirect URI must include exactly:
  http://127.0.0.1:8765/oauth/callback

Usage (on Pi, from nova-hailo root with .env loaded):
  source scripts/setup_env.sh
  python3 scripts/google_oauth_auth.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load .env before oauth config
env_path = ROOT / ".env"
if env_path.is_file():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v

from nova_hailo.google_oauth import (  # noqa: E402
    DEFAULT_REDIRECT_URI,
    GoogleTokenProvider,
    run_local_oauth_flow,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Also open OS default browser (prefer paste into work Chrome).",
    )
    args = parser.parse_args()

    provider = GoogleTokenProvider()
    if provider.config is None:
        print(
            "Missing GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET in .env\n"
            f"Project: {os.getenv('GOOGLE_CLOUD_PROJECT', '(set GOOGLE_CLOUD_PROJECT)')}\n"
            f"Add redirect URI on your Web client: {DEFAULT_REDIRECT_URI}",
            file=sys.stderr,
        )
        return 1

    if not provider.authenticated():
        print(f"Starting OAuth (redirect {provider.config.redirect_uri}) …")
        print("Paste the URL into your *work* Chrome window.")
        info = run_local_oauth_flow(provider, open_browser=args.browser)
        print(f"Saved tokens → {info['path']} (mode 0600)")
        print(f"Scopes: {', '.join(info['scopes'])}")
    else:
        print(f"Already authenticated ({provider.store.path})")
        print("Re-auth: delete the token file or use Settings → Reconnect / --force via UI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
