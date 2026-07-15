"""Unit tests for Google Calendar MCP tools."""
from __future__ import annotations

import time
from pathlib import Path

from nova.tools.mcp.calendar import CheckCalendarTool, _normalize_events
from nova.tools.mcp.oauth import GoogleOAuthConfig, GoogleTokenProvider, TokenStore


def test_token_store_roundtrip_chmod(tmp_path: Path):
    path = tmp_path / "tokens.json"
    store = TokenStore(path)
    store.save(
        {
            "access_token": "ya29.fake",
            "refresh_token": "1//fake",
            "expires_at": time.time() + 3600,
            "scopes": ["https://www.googleapis.com/auth/calendar.events.readonly"],
            "project_id": "test-gcp-project",
        }
    )
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    loaded = store.load()
    assert loaded is not None
    assert loaded["refresh_token"] == "1//fake"


def test_check_calendar_unavailable_without_config():
    tool = CheckCalendarTool(tokens=None)
    out = tool.execute()
    assert out["status"] == "unavailable"
    assert out["reason"] == "google_oauth_not_configured"


def test_check_calendar_unavailable_without_tokens(tmp_path: Path):
    cfg = GoogleOAuthConfig(
        client_id="cid.apps.googleusercontent.com",
        client_secret="csecret",
        project_id="test-gcp-project",
    )
    provider = GoogleTokenProvider(config=cfg, store=TokenStore(tmp_path / "t.json"))
    tool = CheckCalendarTool(tokens=provider)
    out = tool.execute()
    assert out["status"] == "unavailable"
    assert out["reason"] == "google_oauth_not_authenticated"


def test_normalize_events_from_items():
    events = _normalize_events(
        {
            "items": [
                {
                    "summary": "Nova Demo Sync",
                    "start": {"dateTime": "2026-07-15T11:00:00+05:30"},
                },
                {
                    "summary": "Legum Ai",
                    "start": {"dateTime": "2026-07-15T14:30:00+05:30"},
                },
            ]
        }
    )
    assert events[0]["title"] == "Nova Demo Sync"
    assert events[1]["title"] == "Legum Ai"


class _FakeMcp:
    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        return self.result


def _authed_provider(tmp_path: Path) -> GoogleTokenProvider:
    cfg = GoogleOAuthConfig(
        client_id="cid.apps.googleusercontent.com",
        client_secret="csecret",
        project_id="test-gcp-project",
    )
    store = TokenStore(tmp_path / "t.json")
    store.save(
        {
            "access_token": "ya29.fake",
            "refresh_token": "1//fake",
            "expires_at": time.time() + 3600,
            "project_id": "test-gcp-project",
        }
    )
    return GoogleTokenProvider(config=cfg, store=store)


def test_check_calendar_success_via_mcp(tmp_path: Path):
    mcp = _FakeMcp(
        {
            "structuredContent": {
                "events": [
                    {
                        "id": "evt_nova",
                        "summary": "Nova Demo Sync",
                        "start": {"dateTime": "2026-07-15T11:00:00+05:30"},
                    },
                    {
                        "id": "evt_legum",
                        "summary": "Legum Ai",
                        "start": {"dateTime": "2026-07-15T14:30:00+05:30"},
                    },
                ]
            }
        }
    )
    tool = CheckCalendarTool(tokens=_authed_provider(tmp_path), mcp=mcp)
    out = tool.execute()
    assert out["status"] == "success"
    assert out["source"] == "google_calendar_mcp"
    assert [e["title"] for e in out["events"]] == ["Nova Demo Sync", "Legum Ai"]
    assert out["events"][0]["id"] == "evt_nova"
    assert out["event_count"] == 2
    assert "speak" in out
    assert mcp.calls[0][0] == "list_events"
    assert "startTime" in mcp.calls[0][1]
    assert "endTime" in mcp.calls[0][1]


def test_check_calendar_tomorrow_filters_and_speak(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    local_now = datetime.now(timezone.utc).astimezone()
    today = local_now.replace(hour=14, minute=30, second=0, microsecond=0)
    tomorrow = (local_now + timedelta(days=1)).replace(
        hour=11, minute=0, second=0, microsecond=0
    )
    mcp = _FakeMcp(
        {
            "structuredContent": {
                "events": [
                    {
                        "id": "evt_today",
                        "summary": "Today Meet",
                        "start": {"dateTime": today.isoformat()},
                    },
                    {
                        "id": "evt_tmr",
                        "summary": "Test Sync 1",
                        "start": {"dateTime": tomorrow.isoformat()},
                    },
                ]
            }
        }
    )
    tool = CheckCalendarTool(tokens=_authed_provider(tmp_path), mcp=mcp)
    out = tool.execute(day="tomorrow")
    assert out["status"] == "success"
    assert out["day"] == "tomorrow"
    assert [e["title"] for e in out["events"]] == ["Test Sync 1"]
    assert "Test Sync 1" in out["speak"]
    assert "Tomorrow is July" not in out["speak"]


def test_create_calendar_event_via_mcp(tmp_path: Path):
    from nova.tools.mcp.calendar import CreateCalendarEventTool

    mcp = _FakeMcp({"structuredContent": {"id": "new1", "summary": "Standup"}})
    tool = CreateCalendarEventTool(tokens=_authed_provider(tmp_path), mcp=mcp)
    out = tool.execute(title="Standup", start="2026-07-15T15:00:00+05:30")
    assert out["status"] == "success"
    assert out["source"] == "google_calendar_mcp"
    assert out["event"]["id"] == "new1"
    assert mcp.calls[0][0] == "create_event"
    assert mcp.calls[0][1]["summary"] == "Standup"
    assert mcp.calls[0][1]["startTime"] == "2026-07-15T15:00:00+05:30"
    assert "endTime" in mcp.calls[0][1]
