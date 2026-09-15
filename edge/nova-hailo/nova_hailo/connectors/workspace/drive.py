"""Google Drive via OAuth + Workspace Drive MCP, on the shared transport.

Caller: nova_hailo.edge_harness.tool_broker.ToolBroker._mcp_drive.
Ported from nova/tools/mcp/drive.py: every call used to rebuild an MCP
client and redo the `initialize` handshake, which was the single largest
cost in this tool's latency. `_call` now goes through
nova_hailo.connectors.mcp_transport, which caches one handshake per URL and
reuses it for every later call. The parsing and speech helpers below are
copied unchanged -- they are voice-shaped already and are not what was slow.

CreateDriveFolderTool is ported for parity with the source module but is a
write tool: it stays unreachable while tools.write_enabled is false and
policy.OEM_READONLY lists only read tools.
"""
from __future__ import annotations

from typing import Any, Protocol

from nova_hailo.connectors import mcp_transport
from nova_hailo.connectors.workspace.runtime import (
    oauth_ready_or_error,
    unpack_mcp_result,
    unwrap_call_tool_result,
)
from nova_hailo.google_oauth import GoogleTokenProvider

DRIVE_MCP_URL = "https://drivemcp.googleapis.com/mcp/v1"

_UNSET: Any = object()
FOLDER_MIME = "application/vnd.google-apps.folder"


class _McpCaller(Protocol):
    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any: ...


class ListDriveFilesTool:
    """Recent / matching files on the user's Google Drive."""

    name = "list_drive_files"
    description = (
        "List recent files on the user's Google Drive, optionally filtered by query. "
        "Speak the returned speak field verbatim — do not invent file names. "
        "To create a folder use create_drive_folder."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional name substring to search for (e.g. 'budget').",
            },
        },
        "required": [],
    }

    def __init__(
        self,
        tokens: GoogleTokenProvider | None = _UNSET,
        timeout: float = 8.0,
        mcp: _McpCaller | None = None,
    ):
        if tokens is _UNSET:
            self._tokens = GoogleTokenProvider()
        else:
            self._tokens = tokens
        self._timeout = timeout
        # Test seam: an injected caller bypasses mcp_transport (and its
        # process-wide session cache) entirely -- see _call() below.
        self._mcp = mcp

    def _auth_headers(self) -> dict[str, str]:
        # Built fresh per request (never cached) so a token refreshed mid-turn
        # is never masked by a header captured before it rotated.
        token = self._tokens.get_access_token() or ""
        return {"Authorization": f"Bearer {token}"}

    def _call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if self._mcp is not None:
            return self._mcp.call_tool(name, arguments)
        envelope = mcp_transport.call_tool(
            DRIVE_MCP_URL,
            name,
            arguments,
            headers_fn=self._auth_headers,
            timeout=self._timeout,
        )
        return unwrap_call_tool_result(envelope)

    def execute(self, query: str = "") -> dict[str, Any]:
        err = oauth_ready_or_error(self._tokens)
        if err is not None:
            return {
                **err,
                "speak": "Google Drive is unavailable. Reconnect Google in Settings.",
            }

        needle = (query or "").strip()
        for prefix in ("named as ", "named ", "called ", "as "):
            if needle.lower().startswith(prefix):
                needle = needle[len(prefix) :].strip()
                break

        try:
            if needle:
                safe = needle.replace("\\", "\\\\").replace("'", "\\'")
                raw = self._call(
                    "search_files",
                    {"query": f"title contains '{safe}'", "pageSize": 8},
                )
            else:
                raw = self._call(
                    "list_recent_files",
                    {"pageSize": 8, "orderBy": "lastModified"},
                )
            payload = unpack_mcp_result(raw)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "reason": "drive_mcp_error",
                "error": str(exc)[:300],
                "hint": (
                    "If 403 insufficient scopes, Disconnect then Connect Google again "
                    "to grant drive scopes."
                ),
                "speak": "Google Drive is unavailable. Reconnect Google in Settings.",
            }

        files = _normalize_files(payload)
        speak = _speak_files(files, needle)
        return {
            "status": "success",
            "file_count": len(files),
            "files": files,
            "speak": speak,
            "source": "google_drive_mcp",
        }


class CreateDriveFolderTool:
    """Create a folder on Google Drive (irreversible — ConfirmationGate)."""

    name = "create_drive_folder"
    description = (
        "Create a new folder on the user's Google Drive. "
        "Irreversible: confirm the folder name with the driver, then call with confirmed=true."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Folder name to create, e.g. Nova S.",
            },
        },
        "required": ["name"],
    }

    def __init__(
        self,
        tokens: GoogleTokenProvider | None = _UNSET,
        timeout: float = 8.0,
        mcp: _McpCaller | None = None,
    ):
        if tokens is _UNSET:
            self._tokens = GoogleTokenProvider()
        else:
            self._tokens = tokens
        self._timeout = timeout
        self._mcp = mcp

    def _auth_headers(self) -> dict[str, str]:
        token = self._tokens.get_access_token() or ""
        return {"Authorization": f"Bearer {token}"}

    def _call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if self._mcp is not None:
            return self._mcp.call_tool(name, arguments)
        envelope = mcp_transport.call_tool(
            DRIVE_MCP_URL,
            name,
            arguments,
            headers_fn=self._auth_headers,
            timeout=self._timeout,
        )
        return unwrap_call_tool_result(envelope)

    def execute(self, name: str) -> dict[str, Any]:
        err = oauth_ready_or_error(self._tokens)
        if err is not None:
            return {
                **err,
                "speak": (
                    "Could not create the Drive folder. "
                    "Reconnect Google in Settings for write access."
                ),
            }

        folder_name = (name or "").strip()
        if not folder_name:
            return {
                "status": "error",
                "reason": "missing_name",
                "speak": "Need a folder name to create on Drive.",
            }

        try:
            raw = self._call(
                "create_file",
                {"title": folder_name, "mimeType": FOLDER_MIME},
            )
            payload = unpack_mcp_result(raw)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "reason": "drive_mcp_error",
                "error": str(exc)[:300],
                "hint": (
                    "If 403 insufficient scopes, Disconnect then Connect Google again "
                    "to grant drive.file."
                ),
                "speak": (
                    "Could not create the Drive folder. "
                    "Reconnect Google in Settings for write access."
                ),
            }

        fid = str(payload.get("id") or "")
        return {
            "status": "success",
            "action": "created",
            "folder": {
                "id": fid,
                "name": str(payload.get("title") or payload.get("name") or folder_name),
            },
            "speak": f"Created Drive folder {folder_name}.",
            "source": "google_drive_mcp",
        }


def _normalize_files(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw = payload.get("files") or []
    out: list[dict[str, str]] = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        out.append(
            {
                "id": str(f.get("id") or ""),
                "name": str(f.get("title") or f.get("name") or "untitled"),
                "mime_type": str(f.get("mimeType") or f.get("mime_type") or ""),
                "modified": str(
                    f.get("modifiedTime") or f.get("modified_time") or ""
                ),
            }
        )
    return out


def _speak_files(files: list[dict[str, str]], query: str) -> str:
    if not files:
        if query:
            return f"No Drive files matching {query}."
        return "No recent Drive files found."
    names = "; ".join(f.get("name") or "untitled" for f in files[:6])
    more = f" And {len(files) - 6} more." if len(files) > 6 else ""
    prefix = f"Drive files matching {query}" if query else "Recent Drive files"
    return f"{prefix}: {names}.{more}".strip()
