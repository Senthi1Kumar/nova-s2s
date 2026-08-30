"""Google Workspace OAuth for Nova-Hailo (self-contained — no nova.tools.mcp imports).

Callers: nova_hailo.web.app integrations, scripts/google_oauth_auth.py.
Stores tokens under ROOT/runtime/google_oauth/tokens.json (mode 0600).
Redirect URI default: http://127.0.0.1:8765/oauth/callback (GCP Web client).
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

from nova_hailo.config import ROOT

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/oauth/callback"
DEFAULT_CALLBACK_PORT = 8765

# OEM demo: read-only Calendar / Gmail / Drive
OEM_READONLY_SCOPES = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)

DEFAULT_TOKEN_PATH = ROOT / "runtime" / "google_oauth" / "tokens.json"


def _env(name: str) -> str | None:
    v = (os.environ.get(name) or "").strip()
    return v or None


@dataclass
class GoogleOAuthConfig:
    client_id: str
    client_secret: str
    project_id: str
    redirect_uri: str = DEFAULT_REDIRECT_URI
    scopes: tuple[str, ...] = OEM_READONLY_SCOPES

    @classmethod
    def from_env(cls) -> GoogleOAuthConfig | None:
        client_id = _env("GOOGLE_OAUTH_CLIENT_ID")
        client_secret = _env("GOOGLE_OAUTH_CLIENT_SECRET")
        project_id = _env("GOOGLE_CLOUD_PROJECT") or ""
        if not client_id or not client_secret:
            return None
        redirect = _env("GOOGLE_OAUTH_REDIRECT_URI") or DEFAULT_REDIRECT_URI
        extra = _env("GOOGLE_OAUTH_SCOPES")
        scopes = (
            tuple(s.strip() for s in extra.split() if s.strip())
            if extra
            else OEM_READONLY_SCOPES
        )
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            project_id=project_id,
            redirect_uri=redirect,
            scopes=scopes,
        )


class TokenStore:
    def __init__(self, path: Path | str | None = None):
        env_path = _env("GOOGLE_OAUTH_TOKEN_PATH")
        if path:
            self.path = Path(path)
        elif env_path:
            p = Path(env_path)
            self.path = p if p.is_absolute() else (ROOT / p)
        else:
            self.path = DEFAULT_TOKEN_PATH
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
        return str(data.get("project_id") or _env("GOOGLE_CLOUD_PROJECT") or "")

    def _mark_refresh_failed(self, data: dict[str, Any]) -> None:
        """Record that Google rejected our refresh token.

        A stored refresh token that Google has revoked still looks valid to
        ``authenticated()``, which only checks the field is present. Without
        this marker the UI keeps showing "Connected" and the failure surfaces
        as a tool error mid-conversation instead.
        """
        try:
            self.store.save({**data, "refresh_failed_at": datetime.now(timezone.utc).isoformat()})
        except Exception:
            pass

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
            self._mark_refresh_failed(data)
            return None
        access = payload.get("access_token")
        if not access:
            self._mark_refresh_failed(data)
            return None
        updated = {
            **data,
            "access_token": access,
            "token_type": payload.get("token_type", "Bearer"),
            "expires_at": time.time() + int(payload.get("expires_in", 3600)),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        # Only a refresh that actually worked clears a previous failure.
        updated.pop("refresh_failed_at", None)
        if payload.get("refresh_token"):
            updated["refresh_token"] = payload["refresh_token"]
        self.store.save(updated)
        return updated

    def exchange_code(self, code: str) -> dict[str, Any]:
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
                "<title>Nova-Hailo - Google connected</title></head><body style="
                '"font-family:system-ui;background:#10131a;color:#eceef2;'
                "display:flex;min-height:100vh;align-items:center;"
                'justify-content:center;margin:0">'
                '<div style="text-align:center;max-width:28rem;padding:2rem">'
                '<p style="font-size:1.1rem;margin:0 0 .5rem">Google Workspace connected</p>'
                '<p style="color:#8b93a2;margin:0">You can close this tab and return to Nova-Hailo Settings.</p>'
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
    provider = provider or GoogleTokenProvider()
    if provider.config is None:
        raise RuntimeError(
            "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env "
            f"(redirect must be {DEFAULT_REDIRECT_URI})"
        )
    state = secrets.token_urlsafe(24)
    result: dict[str, Any] = {"error": None, "code": None}
    port = _callback_port(provider)
    server = HTTPServer(("127.0.0.1", port), _make_callback_handler(state, result))
    server.timeout = 180
    url = provider.authorization_url(state)
    print(
        "Open this URL in your *work* Chrome profile:\n"
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


_ui_oauth_lock = threading.Lock()
_ui_oauth_state: dict[str, Any] = {"active": False, "error": None}


def begin_ui_oauth_flow(provider: GoogleTokenProvider | None = None) -> dict[str, Any]:
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

        threading.Thread(target=worker, name="hailo-google-oauth-ui", daemon=True).start()
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


def integration_status() -> dict[str, Any]:
    provider = GoogleTokenProvider()
    scopes: list[str] = []
    needs_reauth = False
    if provider.authenticated():
        data = provider.store.load() or {}
        raw = data.get("scopes") or []
        if isinstance(raw, list):
            scopes = [str(s) for s in raw]
        # authenticated() only checks a refresh_token is present. If Google has
        # since revoked it, the last refresh attempt left this marker behind.
        needs_reauth = bool(data.get("refresh_failed_at"))
    return {
        "configured": provider.configured(),
        "authenticated": provider.authenticated(),
        "needs_reauth": needs_reauth,
        "project_id": provider.project_id() if provider.configured() else None,
        "redirect_uri": (provider.config.redirect_uri if provider.config else DEFAULT_REDIRECT_URI),
        "token_path": str(provider.store.path),
        "scopes": scopes,
        "has_calendar": any("calendar" in s for s in scopes),
        "has_gmail": "https://www.googleapis.com/auth/gmail.readonly" in scopes,
        "has_drive": "https://www.googleapis.com/auth/drive.readonly" in scopes,
        "requested_scopes": list(OEM_READONLY_SCOPES),
        "oauth_flow": ui_oauth_status(),
    }
