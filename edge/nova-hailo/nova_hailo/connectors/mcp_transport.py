"""Shared MCP transport: session caching and connection pooling.

Every Google Workspace tool used to redo the full MCP `initialize` handshake
on every single call -- the single largest cost in the tool path, on the
order of seconds per invocation. The handshake is a one-time cost per
endpoint: the server hands back an `Mcp-Session-Id` that stays valid across
many subsequent JSON-RPC requests. This module amortizes that cost by
caching one `httpx.Client` and one session id per URL and reusing both for
every later `call`/`call_tool`, so only the first request to a given MCP
endpoint ever pays for `initialize`.

Not a general-purpose MCP client -- just enough JSON-RPC-over-HTTP to drive
Google's remote MCP servers: lazy `initialize`, `tools/call` and friends,
SSE-or-plain-JSON response bodies, and a bounded one-shot retry when a
cached session has expired server-side. Everything else (OAuth headers,
tool schemas, per-tool timeouts) is the caller's job.
"""
from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Callable
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-11-25"
CLIENT_INFO = {"name": "nova-hailo", "version": "0.1.0"}
DEFAULT_TIMEOUT = 8.0


class McpError(RuntimeError):
    """An MCP server rejected a request.

    Covers a JSON-RPC `error` member, a `result` with `isError: true`, and
    any non-retryable HTTP failure (e.g. HTTP 500).
    """


class _SessionExpiredError(RuntimeError):
    """Internal signal that the cached `Mcp-Session-Id` is no longer valid.

    Never escapes this module: `call` catches it, discards the stale
    session, re-initializes once, and retries -- surfacing `McpError` only
    if that retry also fails.
    """


class _Session:
    """Per-URL MCP connection state: pooled client + session id + init flag.

    `lock` serializes only the initialize check-and-set for this URL, so
    concurrent callers can't both observe `initialized=False` and both fire
    `initialize`. It is released before the JSON-RPC request itself runs --
    `httpx.Client` is safe for concurrent use, so once a session is
    initialized, steady-state calls share it without queuing behind one
    another.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.client = httpx.Client()
        self.session_id: str | None = None
        self.initialized = False
        self.lock = threading.Lock()
        self._ids = itertools.count(1)

    def next_id(self) -> int:
        return next(self._ids)

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


_sessions: dict[str, _Session] = {}
_sessions_lock = threading.Lock()


def _get_session(url: str) -> _Session:
    with _sessions_lock:
        session = _sessions.get(url)
        if session is None:
            session = _Session(url)
            _sessions[url] = session
        return session


def _replace_session(url: str, stale: _Session) -> _Session:
    """Swap `stale` for a fresh `_Session`, but only if it is still cached.

    Guards the concurrent-expiry case: several callers can share one
    session, all observe the same expiry, and all try to replace it. Only
    the caller that still finds `stale` cached for `url` evicts-and-closes
    it (identity-checked, not just by URL); everyone else reuses whatever
    replacement is already there instead of creating -- or closing -- a
    second one. This is what stops a slower thread from closing a sibling
    thread's freshly-initialized replacement out from under it.
    """
    with _sessions_lock:
        current = _sessions.get(url)
        if current is stale:
            new_session = _Session(url)
            _sessions[url] = new_session
            to_close: _Session | None = stale
        else:
            new_session = current if current is not None else _Session(url)
            if current is None:
                _sessions[url] = new_session
            to_close = None
    if to_close is not None:
        to_close.close()
    return new_session


def reset(url: str | None = None) -> None:
    """Drop cached session(s) and close their HTTP clients.

    `url=None` clears every cached session. Test-only reset hook -- module
    state persists for the life of the process otherwise.
    """
    with _sessions_lock:
        if url is None:
            sessions = list(_sessions.values())
            _sessions.clear()
        else:
            session = _sessions.pop(url, None)
            sessions = [session] if session is not None else []
    for session in sessions:
        session.close()


def _headers(headers_fn: Callable[[], dict[str, str]] | None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if headers_fn is not None:
        # Called per request, never cached, so a refreshed OAuth token is
        # never masked by a header captured before it rotated.
        headers.update(headers_fn())
    return headers


def _parse_sse(text: str) -> Any:
    """Concatenate `data:` line payloads and JSON-decode the result."""
    chunks = [
        line[len("data:") :].strip() for line in text.splitlines() if line.startswith("data:")
    ]
    return json.loads("".join(chunks))


def _parse_body(resp: httpx.Response) -> Any:
    ctype = resp.headers.get("content-type", "")
    try:
        if "text/event-stream" in ctype:
            return _parse_sse(resp.text)
        return resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise McpError(f"invalid MCP response body: {exc}") from exc


def _looks_like_expired_session(status_code: int, body_text: str) -> bool:
    if status_code == 404:
        return True
    if status_code == 400 and "session" in body_text.lower():
        return True
    return False


def _join_error_text(result: dict[str, Any]) -> str:
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def _post(
    session: _Session,
    payload: dict[str, Any],
    headers_fn: Callable[[], dict[str, str]] | None,
    timeout: float,
    label: str,
) -> Any:
    """Send one JSON-RPC POST and return its decoded body.

    Raises `_SessionExpiredError` for the expiry signals the brief defines
    (HTTP 404; HTTP 400 whose body mentions "session"; a JSON-RPC `error`
    whose message mentions "session"). Every other failure -- HTTP 500,
    connection timeouts, any other JSON-RPC error -- raises `McpError` (or
    propagates a raw httpx exception) with no retry, so a real outage fails
    fast instead of doubling every timeout.
    """
    headers = _headers(headers_fn)
    if session.session_id:
        headers["Mcp-Session-Id"] = session.session_id
    resp = session.client.post(session.url, headers=headers, json=payload, timeout=timeout)
    sid = resp.headers.get("mcp-session-id")
    if sid:
        session.session_id = sid
    if not resp.is_success:
        if _looks_like_expired_session(resp.status_code, resp.text):
            raise _SessionExpiredError(f"http {resp.status_code}")
        raise McpError(f"{label} failed: http {resp.status_code}: {resp.text[:200]}")
    data = _parse_body(resp)
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        message = err.get("message", "") if isinstance(err, dict) else str(err)
        if "session" in message.lower():
            raise _SessionExpiredError(message)
        raise McpError(f"{label} error: {err}")
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, dict) and result.get("isError"):
            raise McpError(_join_error_text(result) or f"{label} tool error")
    return data


def _ensure_initialized(
    session: _Session,
    headers_fn: Callable[[], dict[str, str]] | None,
    timeout: float,
) -> None:
    """Run the `initialize` handshake for `session`, exactly once.

    Holds `session.lock` for the check-and-set and the handshake itself
    (a one-time cost), but not for the caller's actual RPC that follows --
    see `_Session.lock`.
    """
    with session.lock:
        if session.initialized:
            return
        payload = {
            "jsonrpc": "2.0",
            "id": session.next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        }
        _post(session, payload, headers_fn, timeout, "initialize")
        session.initialized = True
        try:
            notify = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            _post(session, notify, headers_fn, timeout, "notifications/initialized")
        except Exception:  # noqa: BLE001
            pass  # Best-effort: servers that don't ack this must not block us.


def _rpc(
    session: _Session,
    method: str,
    params: dict[str, Any] | None,
    headers_fn: Callable[[], dict[str, str]] | None,
    timeout: float,
) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": session.next_id(),
        "method": method,
        "params": params or {},
    }
    return _post(session, payload, headers_fn, timeout, method)


def call(
    url: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    headers_fn: Callable[[], dict[str, str]] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Send one JSON-RPC request to an MCP server, reusing a cached session.

    Lazily initializes the session on first use per URL. If the cached
    session has expired server-side, discards it, re-initializes exactly
    once, and retries this same request once. Any other failure -- HTTP 500,
    a timeout, a non-session JSON-RPC error -- raises on the first attempt.
    """
    session = _get_session(url)
    try:
        _ensure_initialized(session, headers_fn, timeout)
        return _rpc(session, method, params, headers_fn, timeout)
    except _SessionExpiredError:
        pass  # Fall through to the one allowed retry, below.

    # Retry path: replace only the exact stale object this call observed
    # (identity-checked in `_replace_session`), so callers that hit the same
    # expiry concurrently converge on one shared replacement instead of
    # racing to close each other's freshly-initialized session.
    session = _replace_session(url, session)
    try:
        _ensure_initialized(session, headers_fn, timeout)
        return _rpc(session, method, params, headers_fn, timeout)
    except _SessionExpiredError as exc:
        raise McpError(f"{method} failed after session re-init: {exc}") from exc


def call_tool(
    url: str,
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    headers_fn: Callable[[], dict[str, str]] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Convenience wrapper: `call(url, "tools/call", {...})`."""
    return call(
        url,
        "tools/call",
        {"name": name, "arguments": arguments or {}},
        headers_fn=headers_fn,
        timeout=timeout,
    )
