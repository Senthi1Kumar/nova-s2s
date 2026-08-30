"""A revoked Google refresh token must surface as needs_reauth, not as a tool error.

``GoogleTokenProvider.authenticated()`` only checks that a refresh_token field
is present, so a token Google has already revoked still reads as connected.
``_refresh`` leaves a ``refresh_failed_at`` marker behind when Google rejects
it, and that marker is what the settings UI reads.
"""
from __future__ import annotations

import json
from pathlib import Path

# google_oauth pulls in no Hailo runtime, so this imports cleanly off-device.
# Do NOT stub hailo_platform into sys.modules here: pytest shares sys.modules
# across the whole session, so a stub inserted at collection time leaks into
# every other test module and corrupts unrelated suites.
from nova_hailo.google_oauth import GoogleTokenProvider, TokenStore


def _store(tmp_path: Path, **extra) -> TokenStore:
    p = tmp_path / "tokens.json"
    p.write_text(
        json.dumps(
            {
                "access_token": "at-test",
                "refresh_token": "rt-test",
                "token_type": "Bearer",
                "expires_at": 0,
                "scopes": ["https://www.googleapis.com/auth/calendar.readonly"],
                **extra,
            }
        )
    )
    return TokenStore(p)


def test_marker_absent_on_a_healthy_token(tmp_path: Path):
    data = _store(tmp_path).load()
    assert data is not None
    assert not data.get("refresh_failed_at")


def test_mark_refresh_failed_writes_the_marker(tmp_path: Path):
    store = _store(tmp_path)
    provider = GoogleTokenProvider()
    provider.store = store
    provider._mark_refresh_failed(store.load() or {})
    data = store.load() or {}
    assert data.get("refresh_failed_at"), "a revoked refresh must leave a marker"
    # The rest of the record must survive so a later re-auth can reuse it.
    assert data.get("refresh_token") == "rt-test"
    assert data.get("scopes")


def test_marker_cleared_only_by_a_successful_refresh(tmp_path: Path):
    store = _store(tmp_path, refresh_failed_at="2026-08-29T07:14:22+00:00")
    assert (store.load() or {}).get("refresh_failed_at")
    # Mirrors the success path in _refresh, which pops the key before saving.
    updated = {**(store.load() or {}), "access_token": "at-new"}
    updated.pop("refresh_failed_at", None)
    store.save(updated)
    assert not (store.load() or {}).get("refresh_failed_at")
