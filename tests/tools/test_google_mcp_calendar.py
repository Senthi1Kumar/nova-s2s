"""Unit tests for Google Calendar check_calendar (OAuth + REST)."""
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


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_check_calendar_success_via_rest(tmp_path: Path):
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
    provider = GoogleTokenProvider(config=cfg, store=store)

    def fake_get(url, params=None, headers=None, timeout=None):
        assert "calendar/v3" in url
        assert headers["Authorization"].startswith("Bearer ")
        return _FakeResp(
            {
                "items": [
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
        )

    tool = CheckCalendarTool(tokens=provider, http_get=fake_get)
    out = tool.execute()
    assert out["status"] == "success"
    assert out["source"] == "google_calendar_api"
    assert [e["title"] for e in out["events"]] == ["Nova Demo Sync", "Legum Ai"]
    assert out["events"][0]["id"] == "evt_nova"
    assert out["events"][1]["id"] == "evt_legum"
    assert "speak" in out
    assert out["event_count"] == 2


def test_check_calendar_tomorrow_filters_and_speak(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

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
    provider = GoogleTokenProvider(config=cfg, store=store)

    # Match calendar.py: target day = now_utc.astimezone().date() (+1 for tomorrow).
    # Do not hardcode IST — CI runners are UTC and day boundaries disagree.
    local_now = datetime.now(timezone.utc).astimezone()
    today = local_now.replace(hour=14, minute=30, second=0, microsecond=0)
    tomorrow = (local_now + timedelta(days=1)).replace(
        hour=11, minute=0, second=0, microsecond=0
    )

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResp(
            {
                "items": [
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
        )

    tool = CheckCalendarTool(tokens=provider, http_get=fake_get)
    out = tool.execute(day="tomorrow")
    assert out["status"] == "success"
    assert out["day"] == "tomorrow"
    assert [e["title"] for e in out["events"]] == ["Test Sync 1"]
    assert "Test Sync 1" in out["speak"]
    assert "Tomorrow is July" not in out["speak"]


def test_create_calendar_event_success(tmp_path: Path):
    from nova.tools.mcp.calendar import CreateCalendarEventTool

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
    provider = GoogleTokenProvider(config=cfg, store=store)

    def fake_post(url, json=None, headers=None, timeout=None):
        assert "calendar/v3" in url
        assert json["summary"] == "Standup"
        return _FakeResp(
            {
                "id": "evt_new",
                "summary": "Standup",
                "start": {"dateTime": json["start"]["dateTime"]},
                "end": {"dateTime": json["end"]["dateTime"]},
            }
        )

    tool = CreateCalendarEventTool(tokens=provider, http_post=fake_post)
    out = tool.execute(title="Standup", start="2026-07-15T15:00:00+05:30")
    assert out["status"] == "success"
    assert out["action"] == "created"
    assert out["event"]["id"] == "evt_new"
    assert out["event"]["end"] == "2026-07-15T16:00:00+05:30"


def test_delete_calendar_event_by_title(tmp_path: Path):
    from nova.tools.mcp.calendar import DeleteCalendarEventTool

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
    provider = GoogleTokenProvider(config=cfg, store=store)
    deleted: list[str] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResp(
            {
                "items": [
                    {
                        "id": "evt_legum",
                        "summary": "Legum Ai",
                        "start": {"dateTime": "2026-07-15T14:30:00+05:30"},
                    }
                ]
            }
        )

    def fake_delete(url, headers=None, timeout=None):
        deleted.append(url)
        return _FakeResp({})

    tool = DeleteCalendarEventTool(
        tokens=provider, http_get=fake_get, http_delete=fake_delete
    )
    out = tool.execute(title="Legum")
    assert out["status"] == "success"
    assert out["action"] == "deleted"
    assert out["event"]["id"] == "evt_legum"
    assert "evt_legum" in deleted[0]
