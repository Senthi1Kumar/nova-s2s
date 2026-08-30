"""User-addable connector registry (settings-screen "add your own MCP server").

Pure logic: reads and writes `hmi_settings.json` via `settings_store`, never
opens a network connection itself. Fetching a connector's live tool list is
network I/O and belongs in the web layer (which calls `mcp_client.py`), not
here — this module only stores whatever tool list it is given.

LATENCY CONSTRAINT — do not relax without a fresh measurement: every tool an
*enabled* connector exposes is added to the tool schema sent to the LLM on
every turn. So:
  - `add_connector` always creates a connector with `enabled: False`. Adding
    a connector must never change assistant behaviour on its own.
  - `MAX_CONNECTORS` and `MAX_TOTAL_TOOLS` are hard caps enforced here, not
    just suggested in the UI.

This module does not add connector tools to `openai_tools_for_profile` — that
wiring is a deliberately separate, unbuilt step.
"""
from __future__ import annotations

import secrets
from re import compile as _re_compile
from typing import Any

from nova_hailo import settings_store
from nova_hailo.connectors.url_safety import UnsafeConnectorURLError, assert_public_http_url

MAX_CONNECTORS = 8
MAX_TOTAL_TOOLS = 32
ALLOWED_KINDS = frozenset({"mcp_http"})

_ID_RE = _re_compile(r"^cx_[0-9a-f]{8}$")


class ConnectorError(ValueError):
    """Validation failure or cap violation. Message is safe to show the user."""


class ConnectorNotFoundError(ConnectorError):
    """No connector with that id (including malformed ids)."""


def is_valid_id(cid: str) -> bool:
    return bool(_ID_RE.match(str(cid or "")))


def _new_id(existing: set[str]) -> str:
    for _ in range(100):
        cid = "cx_" + secrets.token_hex(4)
        if cid not in existing:
            return cid
    raise ConnectorError("could not allocate a connector id")


def _normalize_url(url: str) -> str:
    return str(url or "").strip().rstrip("/").lower()


def _validate_label(label: str) -> str:
    label = str(label or "").strip()
    if not label:
        raise ConnectorError("label must not be empty")
    return label


def _validate_url(url: str) -> str:
    """Scheme/host/SSRF check at registration time.

    This alone is not the security boundary: a hostname can resolve
    somewhere public now and somewhere private/loopback/link-local later
    (DNS rebinding), so `mcp_client._rpc_call` re-runs the identical check
    immediately before every outbound network call, independent of this one.
    """
    try:
        return assert_public_http_url(url)
    except UnsafeConnectorURLError as exc:
        raise ConnectorError(str(exc)) from exc


def _validate_kind(kind: str) -> str:
    kind = str(kind or "mcp_http").strip().lower()
    if kind not in ALLOWED_KINDS:
        raise ConnectorError(f"unsupported connector kind: {kind}")
    return kind


def list_connectors(path: Any = None) -> list[dict[str, Any]]:
    """All connectors as stored, enabled and disabled alike."""
    data = settings_store.load_settings(path)
    return [dict(c) for c in data.get("connectors") or []]


def _save(connectors: list[dict[str, Any]], path: Any = None) -> None:
    settings_store.save_settings({"connectors": connectors}, path)


def _find(connectors: list[dict[str, Any]], cid: str) -> dict[str, Any] | None:
    for c in connectors:
        if c.get("id") == cid:
            return c
    return None


def _find_or_raise(connectors: list[dict[str, Any]], cid: str) -> dict[str, Any]:
    if not is_valid_id(cid):
        raise ConnectorNotFoundError(f"invalid connector id: {cid}")
    connector = _find(connectors, cid)
    if connector is None:
        raise ConnectorNotFoundError(f"no connector with id: {cid}")
    return connector


def _enabled_tool_total(connectors: list[dict[str, Any]], exclude_id: str | None = None) -> int:
    total = 0
    for c in connectors:
        if c.get("id") == exclude_id:
            continue
        if c.get("enabled"):
            total += len(c.get("tools") or [])
    return total


def total_enabled_tools(path: Any = None) -> int:
    return _enabled_tool_total(list_connectors(path))


def add_connector(
    label: str, url: str, kind: str = "mcp_http", path: Any = None
) -> dict[str, Any]:
    """Create a new connector. Always disabled — see module docstring."""
    kind = _validate_kind(kind)
    label = _validate_label(label)
    url = _validate_url(url)

    connectors = list_connectors(path)
    if len(connectors) >= MAX_CONNECTORS:
        raise ConnectorError(f"connector limit reached ({MAX_CONNECTORS})")

    norm = _normalize_url(url)
    for c in connectors:
        if _normalize_url(c.get("url", "")) == norm:
            raise ConnectorError("a connector with this url already exists")

    cid = _new_id({c["id"] for c in connectors if c.get("id")})
    connector = {
        "id": cid,
        "kind": kind,
        "label": label,
        "url": url,
        "enabled": False,
        "tools": [],
    }
    connectors.append(connector)
    _save(connectors, path)
    return dict(connector)


def remove_connector(cid: str, path: Any = None) -> bool:
    connectors = list_connectors(path)
    kept = [c for c in connectors if c.get("id") != cid]
    if len(kept) == len(connectors):
        return False
    _save(kept, path)
    return True


def enable_connector(cid: str, path: Any = None) -> dict[str, Any]:
    """Flip a connector on. Rejected if it would push total exposed tools over
    MAX_TOTAL_TOOLS — the cost of enabling must be paid for explicitly.

    The cap check always runs, even when the connector is already enabled:
    a caller may re-enable an already-enabled connector right after its tool
    list was refreshed (see `app.py`'s enable handler), and that refreshed
    list can be larger than what was checked the first time. Re-checking
    here is a second line of defense on top of `set_tools`'s own guard.
    """
    connectors = list_connectors(path)
    connector = _find_or_raise(connectors, cid)
    projected = _enabled_tool_total(connectors, exclude_id=cid) + len(connector.get("tools") or [])
    if projected > MAX_TOTAL_TOOLS:
        raise ConnectorError(
            f"enabling {connector.get('label') or cid} would expose {projected} tools, "
            f"over the cap of {MAX_TOTAL_TOOLS}"
        )
    if not connector.get("enabled"):
        connector["enabled"] = True
        _save(connectors, path)
    return dict(connector)


def disable_connector(cid: str, path: Any = None) -> dict[str, Any]:
    connectors = list_connectors(path)
    connector = _find_or_raise(connectors, cid)
    connector["enabled"] = False
    _save(connectors, path)
    return dict(connector)


def set_tools(cid: str, tools: list[dict[str, Any]] | None, path: Any = None) -> dict[str, Any]:
    """Record a connector's discovered tool list (from mcp_client.list_tools).

    Cap-aware for an ALREADY-enabled connector: if the new tool list would
    push the total exposed tool count over MAX_TOTAL_TOOLS, the connector is
    auto-disabled rather than left enabled-and-over-cap. A connector's own
    server growing its advertised tools after being enabled must not
    silently blow past the cap just because nobody happened to call
    `enable_connector` again to re-check it.
    """
    connectors = list_connectors(path)
    connector = _find_or_raise(connectors, cid)
    connector["tools"] = [t for t in (tools or []) if isinstance(t, dict) and t.get("name")]
    if connector.get("enabled"):
        projected = _enabled_tool_total(connectors, exclude_id=cid) + len(connector["tools"])
        if projected > MAX_TOTAL_TOOLS:
            connector["enabled"] = False
    _save(connectors, path)
    return dict(connector)
