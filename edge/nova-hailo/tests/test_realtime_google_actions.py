"""nova.google.connect / nova.google.disconnect websocket actions.

``_google_connect_payload`` and ``_google_disconnect_payload`` are the
module-level helpers realtime_session.py's ``_handle_client`` calls for the
QML driver face's two Integrations actions (see SettingsPanel.qml). Both
mirror the existing REST handlers in web/app.py (``/integrations/google/
auth/start`` and ``/integrations/google/disconnect``) so the websocket and
REST surfaces behave identically. Tested standalone here (no live
WebSocket/pipeline/bench) because neither helper touches RealtimeSession
state -- same rationale as test_realtime_settings_snapshot.py for
``_integrations_snapshot``.
"""
from __future__ import annotations

from nova_hailo.web.realtime_session import (
    _google_connect_payload,
    _google_disconnect_payload,
)


def test_connect_returns_auth_url_on_success(monkeypatch):
    monkeypatch.setattr(
        "nova_hailo.google_oauth.begin_ui_oauth_flow",
        lambda: {
            "status": "pending",
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?state=abc",
            "redirect_uri": "http://127.0.0.1:8765/oauth2callback",
        },
    )

    out = _google_connect_payload()

    assert out == {
        "type": "nova.google.auth_url",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?state=abc",
        "status": "pending",
    }


def test_connect_already_authenticated_returns_no_url(monkeypatch):
    # begin_ui_oauth_flow's own short-circuit for an already-healthy token.
    monkeypatch.setattr(
        "nova_hailo.google_oauth.begin_ui_oauth_flow",
        lambda: {
            "status": "already_authenticated",
            "auth_url": None,
            "redirect_uri": "http://127.0.0.1:8765/oauth2callback",
        },
    )

    out = _google_connect_payload()

    assert out["type"] == "nova.google.auth_url"
    assert out["auth_url"] is None
    assert out["status"] == "already_authenticated"


def test_connect_failure_degrades_to_an_error_reply_not_a_raise(monkeypatch):
    def boom():
        raise RuntimeError("google_oauth_not_configured")

    monkeypatch.setattr("nova_hailo.google_oauth.begin_ui_oauth_flow", boom)

    out = _google_connect_payload()

    assert out["type"] == "nova.google.auth_url"
    assert out["auth_url"] is None
    assert "google_oauth_not_configured" in out["error"]


def test_disconnect_clears_the_token_store(monkeypatch):
    cleared = {"called": False}

    class FakeStore:
        def clear(self):
            cleared["called"] = True

    class FakeProvider:
        def __init__(self):
            self.store = FakeStore()

    monkeypatch.setattr("nova_hailo.google_oauth.GoogleTokenProvider", FakeProvider)

    out = _google_disconnect_payload()

    assert out == {"type": "nova.google.disconnected"}
    assert cleared["called"] is True


def test_disconnect_failure_is_swallowed_not_raised(monkeypatch):
    class BoomProvider:
        def __init__(self):
            raise RuntimeError("token store unreadable")

    monkeypatch.setattr("nova_hailo.google_oauth.GoogleTokenProvider", BoomProvider)

    out = _google_disconnect_payload()

    assert out == {"type": "nova.google.disconnected"}
