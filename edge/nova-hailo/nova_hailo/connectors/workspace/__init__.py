"""Google Workspace MCP tools on the shared transport.

Gmail, Calendar, and Drive (this package's `gmail.py`, `calendar.py`, and
`drive.py`) share `runtime.py` for OAuth gating and MCP result parsing, and
`nova_hailo.connectors.mcp_transport` for the handshake, per-URL session
caching, and SSE parsing that used to be redone -- and repaid in full -- on
every single tool call.
"""
from __future__ import annotations
