#!/usr/bin/env python3
"""One-shot Google Workspace MCP OAuth login for Nova.

Caller: operator (manual). Uses nova.tools.mcp.oauth + client.
Writes runtime/google_oauth/tokens.json (0600): access_token, refresh_token,
expires_at, scopes, project_id, updated_at.

Prereq — Web OAuth client redirect URI must include exactly:
  http://127.0.0.1:8765/oauth/callback
"""
from __future__ import annotations

import os

import argparse
import sys

from nova.tools.mcp.client import CALENDAR_MCP_URL, GoogleMcpClient
from nova.tools.mcp.oauth import (
    DEFAULT_REDIRECT_URI,
    GoogleTokenProvider,
    run_local_oauth_flow,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Also open the OS default browser (usually wrong profile — prefer paste into work Chrome).",
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
        print(f"Starting OAuth (redirect {DEFAULT_REDIRECT_URI}) …")
        print("Paste the URL into your *work* Chrome window (not personal Firefox).")
        info = run_local_oauth_flow(provider, open_browser=args.browser)
        print(f"Saved tokens → {info['path']} (mode 0600)")
        print(f"Scopes: {', '.join(info['scopes'])}")
    else:
        print(f"Already authenticated ({provider.store.path})")

    try:
        client = GoogleMcpClient(CALENDAR_MCP_URL, tokens=provider)
        tools = client.list_tools()
        names = [t.get("name", "?") for t in tools]
        print(f"Calendar MCP tools ({len(names)}): {', '.join(names)}")
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: tools/list failed: {exc}", file=sys.stderr)
        print(
            "Check: calendarmcp.googleapis.com enabled, IAM roles/mcp.toolUser, "
            "OAuth scopes on consent screen.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
