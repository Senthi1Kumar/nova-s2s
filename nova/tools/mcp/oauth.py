"""OAuth 2.0 for Google Workspace remote MCP servers.

Stores tokens under ``runtime/google_oauth/`` (gitignored, mode 0600).
Client ID/secret come from env — never committed. Designed for a Web OAuth
client with a fixed localhost redirect (Nova owns the callback, not Antigravity).

Callers: ``nova/tools/mcp/client.py``, ``nova/tools/mcp/calendar.py``,
``scripts/google_mcp_auth.py``. No prior Google OAuth module in this tree.
Token file fields: access_token, refresh_token, expires_at (unix float),
scopes, project_id, token_type, updated_at (ISO-8601 UTC).
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx

from nova.tools._env import get_api_key

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"

# Fixed callback for the Web OAuth client — add this exact URI in GCP console.
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/oauth/callback"
DEFAULT_CALLBACK_PORT = 8765

# Read-only legacy (MCP consent docs). Prefer CALENDAR_SCOPES for create/delete.
CALENDAR_READ_SCOPES = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
    "https://www.googleapis.com/auth/calendar.events.readonly",
)

# calendar.events covers list/create/update/delete; keep freebusy + list.readonly.
# Gmail/Drive scopes for Workspace inbox + files (MCP tools/call).
CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
    "https://www.googleapis.com/auth/calendar.events",
)

GOOGLE_WORKSPACE_SCOPES = CALENDAR_SCOPES + (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",  # create folders/files this app makes
)

DEFAULT_TOKEN_PATH = Path("runtime") / "google_oauth" / "tokens.json"


@dataclass
class GoogleOAuthConfig:
    client_id: str
    client_secret: str
    project_id: str
    redirect_uri: str = DEFAULT_REDIRECT_URI
    scopes: tuple[str, ...] = GOOGLE_WORKSPACE_SCOPES

    @classmethod
    def from_env(cls) -> GoogleOAuthConfig | None:
        client_id = get_api_key("GOOGLE_OAUTH_CLIENT_ID")
        client_secret = get_api_key("GOOGLE_OAUTH_CLIENT_SECRET")
        project_id = get_api_key("GOOGLE_CLOUD_PROJECT") or ""
        if not client_id or not client_secret:
            return None
        redirect = get_api_key("GOOGLE_OAUTH_REDIRECT_URI") or DEFAULT_REDIRECT_URI
        extra = get_api_key("GOOGLE_OAUTH_SCOPES")
        scopes = (
            tuple(s.strip() for s in extra.split() if s.strip())
            if extra
            else GOOGLE_WORKSPACE_SCOPES
        )
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            project_id=project_id,
            redirect_uri=redirect,
            scopes=scopes,
        )


class TokenStore:
    """JSON token file with restrictive permissions. Never logs token values."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else Path(
            get_api_key("GOOGLE_OAUTH_TOKEN_PATH") or DEFAULT_TOKEN_PATH
        )
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.path.is_file():
                return None
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            if not isinstance(data, dict) or not data.get("refresh_token"):
                return None
            return data

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)
            os.chmod(self.path, 0o600)

    def clear(self) -> None:
        with self._lock:
            if self.path.is_file():
                self.path.unlink()


class GoogleTokenProvider:
    """Access-token provider with automatic refresh."""

    def __init__(
        self,
        config: GoogleOAuthConfig | None = None,
        store: TokenStore | None = None,
        timeout: float = 15.0,
    ):
        self.config = config if config is not None else GoogleOAuthConfig.from_env()
        self.store = store or TokenStore()
        self.timeout = timeout
        self._lock = threading.Lock()

    def configured(self) -> bool:
        return self.config is not None

    def authenticated(self) -> bool:
        return self.configured() and self.store.load() is not None

    def get_access_token(self) -> str | None:
        if self.config is None:
            return None
        with self._lock:
            data = self.store.load()
            if data is None:
                return None
            expires_at = float(data.get("expires_at", 0))
            if data.get("access_token") and time.time() < expires_at - 60:
                return str(data["access_token"])
            refreshed = self._refresh(data)
            if refreshed is None:
                return None
            return str(refreshed["access_token"])

    def project_id(self) -> str:
        if self.config is not None:
            return self.config.project_id
        data = self.store.load() or {}
        return str(data.get("project_id") or get_api_key("GOOGLE_CLOUD_PROJECT") or "")

    def _refresh(self, data: dict[str, Any]) -> dict[str, Any] | None:
        assert self.config is not None
        refresh = data.get("refresh_token")
        if not refresh:
            return None
        try:
            resp = httpx.post(
                TOKEN_URI,
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "refresh_token": refresh,
                    "grant_type": "refresh_token",
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError, KeyError):
            return None
        access = payload.get("access_token")
        if not access:
            return None
        updated = {
            **data,
            "access_token": access,
            "token_type": payload.get("token_type", "Bearer"),
            "expires_at": time.time() + int(payload.get("expires_in", 3600)),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if payload.get("refresh_token"):
            updated["refresh_token"] = payload["refresh_token"]
        self.store.save(updated)
        return updated

    def exchange_code(self, code: str) -> dict[str, Any]:
        """Exchange an authorization code for tokens and persist them."""
        if self.config is None:
            raise RuntimeError("GOOGLE_OAUTH_CLIENT_ID / SECRET not set")
        resp = httpx.post(
            TOKEN_URI,
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self.config.redirect_uri,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        if "refresh_token" not in payload:
            existing = self.store.load() or {}
            if existing.get("refresh_token"):
                payload["refresh_token"] = existing["refresh_token"]
            else:
                raise RuntimeError(
                    "No refresh_token returned. Revoke prior grants at "
                    "https://myaccount.google.com/permissions and re-run with prompt=consent."
                )
        data = {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "token_type": payload.get("token_type", "Bearer"),
            "expires_at": time.time() + int(payload.get("expires_in", 3600)),
            "scopes": list(self.config.scopes),
            "project_id": self.config.project_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.save(data)
        return {"status": "ok", "scopes": data["scopes"], "path": str(self.store.path)}

    def authorization_url(self, state: str) -> str:
        if self.config is None:
            raise RuntimeError("GOOGLE_OAUTH_CLIENT_ID / SECRET not set")
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.config.scopes),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{AUTH_URI}?{urllib.parse.urlencode(params)}"


def _callback_port(provider: GoogleTokenProvider) -> int:
    assert provider.config is not None
    host = urllib.parse.urlparse(provider.config.redirect_uri)
    return host.port or DEFAULT_CALLBACK_PORT


def _make_callback_handler(state: str, result: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/oauth/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            if qs.get("state", [None])[0] != state:
                result["error"] = "state_mismatch"
                body = b"OAuth state mismatch. Close this tab."
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if "error" in qs:
                result["error"] = qs["error"][0]
                body = f"OAuth error: {result['error']}".encode()
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            result["code"] = qs.get("code", [None])[0]
            html = (
                "<!doctype html><html><head><meta charset=utf-8>"
                "<title>Nova - Google connected</title></head><body style="
                '"font-family:system-ui;background:#10131a;color:#eceef2;'
                "display:flex;min-height:100vh;align-items:center;"
                'justify-content:center;margin:0">'
                '<div style="text-align:center;max-width:28rem;padding:2rem">'
                '<p style="font-size:1.1rem;margin:0 0 .5rem">Google Workspace connected</p>'
                '<p style="color:#8b93a2;margin:0">You can close this tab and return to Nova Settings.</p>'
                "</div></body></html>"
            )
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    return Handler


def run_local_oauth_flow(
    provider: GoogleTokenProvider | None = None,
    open_browser: bool = False,
) -> dict[str, Any]:
    """Blocking localhost callback. Does not open a browser by default — paste
    the printed URL into your work Chrome profile (avoids personal Firefox)."""
    provider = provider or GoogleTokenProvider()
    if provider.config is None:
        raise RuntimeError(
            "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env "
            f"(set GOOGLE_CLOUD_PROJECT). Redirect URI must be {DEFAULT_REDIRECT_URI}"
        )

    state = secrets.token_urlsafe(24)
    result: dict[str, Any] = {"error": None, "code": None}
    port = _callback_port(provider)
    server = HTTPServer(("127.0.0.1", port), _make_callback_handler(state, result))
    server.timeout = 180
    url = provider.authorization_url(state)
    print(
        "Open this URL in your *work* Chrome profile (do not use the default browser):\n"
        f"{url}\n"
    )
    if open_browser:
        webbrowser.open(url)

    deadline = time.time() + 180
    while time.time() < deadline and result["code"] is None and result["error"] is None:
        server.handle_request()
    server.server_close()

    if result["error"]:
        raise RuntimeError(f"OAuth failed: {result['error']}")
    if not result["code"]:
        raise RuntimeError("OAuth timed out waiting for callback")
    return provider.exchange_code(result["code"])


# In-flight UI OAuth (Settings → Connect). One listener at a time.
_ui_oauth_lock = threading.Lock()
_ui_oauth_state: dict[str, Any] = {"active": False, "error": None}


def begin_ui_oauth_flow(provider: GoogleTokenProvider | None = None) -> dict[str, Any]:
    """Non-blocking OAuth for the voice UI: start callback listener, return auth_url.

    The UI opens ``auth_url`` with ``window.open`` so sign-in stays in the same
    Chrome profile as Nova (not the OS default browser).
    """
    provider = provider or GoogleTokenProvider()
    if provider.config is None:
        raise RuntimeError("google_oauth_not_configured")
    if provider.authenticated():
        return {
            "status": "already_authenticated",
            "auth_url": None,
            "redirect_uri": provider.config.redirect_uri,
        }

    with _ui_oauth_lock:
        if _ui_oauth_state.get("active"):
            url = _ui_oauth_state.get("auth_url")
            if url:
                return {
                    "status": "pending",
                    "auth_url": url,
                    "redirect_uri": provider.config.redirect_uri,
                }

        state = secrets.token_urlsafe(24)
        result: dict[str, Any] = {"error": None, "code": None}
        port = _callback_port(provider)
        try:
            server = HTTPServer(("127.0.0.1", port), _make_callback_handler(state, result))
        except OSError as exc:
            raise RuntimeError(f"oauth_callback_port_busy:{port}: {exc}") from exc
        server.timeout = 1.0
        url = provider.authorization_url(state)
        _ui_oauth_state.clear()
        _ui_oauth_state.update(
            {"active": True, "auth_url": url, "error": None, "done": False}
        )

        def worker() -> None:
            deadline = time.time() + 180
            try:
                while time.time() < deadline and result["code"] is None and result["error"] is None:
                    server.handle_request()
                if result["error"]:
                    _ui_oauth_state["error"] = result["error"]
                elif result["code"]:
                    provider.exchange_code(result["code"])
                    _ui_oauth_state["done"] = True
                else:
                    _ui_oauth_state["error"] = "timeout"
            except Exception as exc:  # noqa: BLE001
                _ui_oauth_state["error"] = str(exc)[:200]
            finally:
                try:
                    server.server_close()
                except Exception:  # noqa: BLE001
                    pass
                _ui_oauth_state["active"] = False
                _ui_oauth_state.pop("auth_url", None)

        threading.Thread(target=worker, name="google-oauth-ui", daemon=True).start()
        return {
            "status": "pending",
            "auth_url": url,
            "redirect_uri": provider.config.redirect_uri,
        }


def ui_oauth_status() -> dict[str, Any]:
    return {
        "active": bool(_ui_oauth_state.get("active")),
        "done": bool(_ui_oauth_state.get("done")),
        "error": _ui_oauth_state.get("error"),
    }