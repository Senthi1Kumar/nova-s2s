"""nova.settings must carry read-only Google + connector status for the QML
driver face, and a failure to read either must never break session startup.

``_integrations_snapshot`` is the module-level helper realtime_session.py
merges into every ``nova.settings`` payload it sends (connect, LLM-switch,
and explicit ``nova.settings.get``). It is tested standalone here rather than
via a full ``RealtimeSession`` (which needs a live websocket/pipeline/bench)
because the logic under test has no dependency on any of that.
"""
from __future__ import annotations

import pytest

from nova_hailo.web.realtime_session import _integrations_snapshot


def test_shape_with_healthy_google_and_enabled_connectors(monkeypatch):
    monkeypatch.setattr(
        "nova_hailo.google_oauth.integration_status",
        lambda: {"authenticated": True, "needs_reauth": False},
    )
    monkeypatch.setattr(
        "nova_hailo.connectors.registry.list_connectors",
        lambda: [
            {"id": "cx_1", "enabled": True, "tools": [{"name": "a"}, {"name": "b"}]},
            {"id": "cx_2", "enabled": False, "tools": [{"name": "c"}]},
        ],
    )
    monkeypatch.setattr("nova_hailo.connectors.registry.total_enabled_tools", lambda: 2)

    out = _integrations_snapshot()

    assert out == {
        "google": {"connected": True, "needs_reauth": False},
        "connectors": {"enabled": 1, "tools": 2},
    }


def test_needs_reauth_wins_over_a_stale_authenticated_flag(monkeypatch):
    # google_oauth.integration_status: the stored token still looks present
    # (authenticated=True) even after Google revoked it — needs_reauth is the
    # only reliable signal, and "connected" must not lie about that.
    monkeypatch.setattr(
        "nova_hailo.google_oauth.integration_status",
        lambda: {"authenticated": True, "needs_reauth": True},
    )
    monkeypatch.setattr("nova_hailo.connectors.registry.list_connectors", lambda: [])
    monkeypatch.setattr("nova_hailo.connectors.registry.total_enabled_tools", lambda: 0)

    out = _integrations_snapshot()

    assert out["google"] == {"connected": False, "needs_reauth": True}


def test_never_authenticated_reports_disconnected(monkeypatch):
    monkeypatch.setattr(
        "nova_hailo.google_oauth.integration_status",
        lambda: {"authenticated": False, "needs_reauth": False},
    )
    monkeypatch.setattr("nova_hailo.connectors.registry.list_connectors", lambda: [])
    monkeypatch.setattr("nova_hailo.connectors.registry.total_enabled_tools", lambda: 0)

    out = _integrations_snapshot()

    assert out["google"] == {"connected": False, "needs_reauth": False}


def test_google_lookup_failure_degrades_to_safe_default(monkeypatch):
    def boom():
        raise RuntimeError("token store unreadable")

    monkeypatch.setattr("nova_hailo.google_oauth.integration_status", boom)
    monkeypatch.setattr(
        "nova_hailo.connectors.registry.list_connectors",
        lambda: [{"id": "cx_1", "enabled": True, "tools": [{"name": "a"}]}],
    )
    monkeypatch.setattr("nova_hailo.connectors.registry.total_enabled_tools", lambda: 1)

    out = _integrations_snapshot()

    assert out["google"] == {"connected": False, "needs_reauth": False}
    # A broken Google lookup must not take the connector status down with it.
    assert out["connectors"] == {"enabled": 1, "tools": 1}


def test_connector_lookup_failure_degrades_to_safe_default(monkeypatch):
    monkeypatch.setattr(
        "nova_hailo.google_oauth.integration_status",
        lambda: {"authenticated": True, "needs_reauth": False},
    )

    def boom():
        raise RuntimeError("settings file corrupt")

    monkeypatch.setattr("nova_hailo.connectors.registry.list_connectors", boom)
    monkeypatch.setattr("nova_hailo.connectors.registry.total_enabled_tools", boom)

    out = _integrations_snapshot()

    assert out["connectors"] == {"enabled": 0, "tools": 0}
    # A broken connector lookup must not take the Google status down with it.
    assert out["google"] == {"connected": True, "needs_reauth": False}


def test_both_lookups_failing_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("nope")

    monkeypatch.setattr("nova_hailo.google_oauth.integration_status", boom)
    monkeypatch.setattr("nova_hailo.connectors.registry.list_connectors", boom)
    monkeypatch.setattr("nova_hailo.connectors.registry.total_enabled_tools", boom)

    out = _integrations_snapshot()

    assert out == {
        "google": {"connected": False, "needs_reauth": False},
        "connectors": {"enabled": 0, "tools": 0},
    }


@pytest.mark.parametrize("bad_status", [None, {}, "not-a-dict"])
def test_missing_or_unexpected_status_shape_is_tolerated(monkeypatch, bad_status):
    monkeypatch.setattr(
        "nova_hailo.google_oauth.integration_status",
        lambda: bad_status,
    )
    monkeypatch.setattr("nova_hailo.connectors.registry.list_connectors", lambda: [])
    monkeypatch.setattr("nova_hailo.connectors.registry.total_enabled_tools", lambda: 0)

    out = _integrations_snapshot()

    assert out["google"] == {"connected": False, "needs_reauth": False}
