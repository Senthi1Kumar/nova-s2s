"""Google Drive via OAuth + Drive JSON API.

Caller: nova/server/tool_service.py build_registry.
List is read-only; create_drive_folder needs drive.file (re-auth if missing).
"""
from __future__ import annotations

from typing import Any, Callable

import httpx

from nova.tools.base import NovaTool
from nova.tools.mcp.calendar import _oauth_access_or_error
from nova.tools.mcp.oauth import GoogleTokenProvider

_UNSET: Any = object()
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"


class ListDriveFilesTool(NovaTool):
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
        timeout: float = 20.0,
        http_get=None,
    ):
        if tokens is _UNSET:
            self._tokens = GoogleTokenProvider()
        else:
            self._tokens = tokens
        self._timeout = timeout
        self._http_get = http_get or httpx.get

    def execute(self, query: str = "") -> dict[str, Any]:
        access = _oauth_access_or_error(self._tokens)
        if isinstance(access, dict):
            return access

        q_parts = ["trashed=false"]
        needle = (query or "").strip()
        for prefix in ("named as ", "named ", "called ", "as "):
            if needle.lower().startswith(prefix):
                needle = needle[len(prefix) :].strip()
                break
        if needle:
            safe = needle.replace("\\", "\\\\").replace("'", "\\'")
            q_parts.append(f"name contains '{safe}'")
        params = {
            "pageSize": 8,
            "fields": "files(id,name,mimeType,modifiedTime)",
            "orderBy": "modifiedTime desc",
            "q": " and ".join(q_parts),
        }
        try:
            resp = self._http_get(
                DRIVE_FILES_URL,
                params=params,
                headers={"Authorization": f"Bearer {access}"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            files = [
                {
                    "id": str(f.get("id") or ""),
                    "name": str(f.get("name") or "untitled"),
                    "mime_type": str(f.get("mimeType") or ""),
                    "modified": str(f.get("modifiedTime") or ""),
                }
                for f in (resp.json().get("files") or [])
                if isinstance(f, dict)
            ]
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "reason": "drive_api_error",
                "error": str(exc)[:300],
                "hint": (
                    "If 403 insufficient scopes, Disconnect then Connect Google again "
                    "to grant drive scopes."
                ),
                "speak": "Google Drive is unavailable. Reconnect Google in Settings.",
            }

        speak = _speak_files(files, needle)
        return {
            "status": "success",
            "file_count": len(files),
            "files": files,
            "speak": speak,
        }


class CreateDriveFolderTool(NovaTool):
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
        timeout: float = 20.0,
        http_post: Callable[..., Any] | None = None,
    ):
        if tokens is _UNSET:
            self._tokens = GoogleTokenProvider()
        else:
            self._tokens = tokens
        self._timeout = timeout
        self._http_post = http_post or httpx.post

    def execute(self, name: str) -> dict[str, Any]:
        access = _oauth_access_or_error(self._tokens)
        if isinstance(access, dict):
            return access

        folder_name = (name or "").strip()
        if not folder_name:
            return {
                "status": "error",
                "reason": "missing_name",
                "speak": "Need a folder name to create on Drive.",
            }

        try:
            resp = self._http_post(
                DRIVE_FILES_URL,
                json={"name": folder_name, "mimeType": FOLDER_MIME},
                headers={
                    "Authorization": f"Bearer {access}",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "reason": "drive_api_error",
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
            "folder": {"id": fid, "name": str(payload.get("name") or folder_name)},
            "speak": f"Created Drive folder {folder_name}.",
        }


def _speak_files(files: list[dict[str, str]], query: str) -> str:
    if not files:
        if query:
            return f"No Drive files matching {query}."
        return "No recent Drive files found."
    names = "; ".join(f.get("name") or "untitled" for f in files[:6])
    more = f" And {len(files) - 6} more." if len(files) > 6 else ""
    prefix = f"Drive files matching {query}" if query else "Recent Drive files"
    return f"{prefix}: {names}.{more}".strip()
