"""Shared fixtures for integration tests (tool service + DriveAuth mock)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nova.server.driveauth_bridge import reset_auth_for_tests
from nova.server.tool_service import app, build_registry, get_registry
from nova.tools.vehicle import VehicleDB


@pytest.fixture()
def tool_client(tmp_path, monkeypatch):
    """Isolated tool-service TestClient with mock DriveAuth."""
    monkeypatch.setenv("DRIVEAUTH_USE_MOCK", "1")
    monkeypatch.setenv("DRIVEAUTH_SEED_MATURE", "1")
    monkeypatch.setenv("DRIVEAUTH_STORE_DIR", str(tmp_path / "da_store"))
    monkeypatch.setenv("DRIVEAUTH_DRIVER_ID", "driver1")
    monkeypatch.setenv("NOVA_TOOL_ROUTE_MODE", "force")
    monkeypatch.setenv("NOVA_ROUTER_LLM_URL", "http://127.0.0.1:9/v1")  # unused in force
    reset_auth_for_tests()
    reg = build_registry(
        VehicleDB(tmp_path / "vehicle.db"),
        driveauth_store=tmp_path / "da_store",
    )
    app.dependency_overrides[get_registry] = lambda: reg
    # Clear sticky confirm state between tests.
    from nova.server import tool_service as ts

    ts._pending_confirm.clear()
    client = TestClient(app)
    yield client, reg
    app.dependency_overrides.clear()
    ts._pending_confirm.clear()
    reset_auth_for_tests()
