"""Tests for the standalone tool service (``GET /tools/schemas``,
``POST /tools/execute``), plus the reusable ``ConfirmationGate`` wrapper.

Uses a real, temp-file-backed ``VehicleDB`` (via a dependency override), not
a mock, per the brief's "no mocking real DB/tool behavior" constraint.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nova.tools.base import NovaTool
from nova.tools.vehicle import SetReminderTool, VehicleDB
from nova.server.tool_service import (
    ConfirmationGate,
    app,
    build_registry,
    get_registry,
    _pending_confirm,
)


class _RaisesValueErrorTool(NovaTool):
    """Throwaway tool whose ``execute()`` raises something other than
    ``TypeError``, to exercise the catch-all 500 path in ``execute_tool``."""

    name = "raises_value_error"
    description = "Test-only tool that always raises ValueError."
    parameters = {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        raise ValueError("boom")


@pytest.fixture
def client(tmp_path):
    registry = build_registry(VehicleDB(tmp_path / "vehicle.db"))
    app.dependency_overrides[get_registry] = lambda: registry
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_schemas_returns_well_formed_function_tool_for_every_registered_tool(client):
    response = client.get("/tools/schemas")

    assert response.status_code == 200
    schemas = response.json()
    assert len(schemas) >= 10  # 7 vehicle + websearch + weather + research + 3 stubs
    names = {s["name"] for s in schemas}
    assert {"set_hvac", "query_vehicle_status", "web_search", "get_weather", "check_email"} <= names
    for schema in schemas:
        assert schema["type"] == "function"
        assert schema["name"]
        assert schema["description"]
        assert schema["parameters"]["type"] == "object"


def test_execute_dispatches_to_real_vehicle_tool_and_persists(client):
    response = client.post(
        "/tools/execute", json={"name": "set_hvac", "args": {"zone": "driver", "on": True, "target_temp_c": 19}}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"

    status_response = client.post("/tools/execute", json={"name": "query_vehicle_status", "args": {}})
    status = status_response.json()
    assert status["result"]["hvac_zones"]["driver"] == "on"
    assert status["result"]["zone_temp_c"]["driver"] == 19.0


def test_execute_unknown_tool_returns_structured_404(client):
    response = client.post("/tools/execute", json={"name": "not_a_real_tool", "args": {}})

    assert response.status_code == 404
    assert response.json() == {"error": "unknown tool: not_a_real_tool"}


def test_execute_bad_args_returns_structured_422(client):
    response = client.post("/tools/execute", json={"name": "set_hvac", "args": {"bogus_kwarg": True}})

    assert response.status_code == 422
    assert "error" in response.json()


def test_execute_tool_raising_non_type_error_returns_structured_500(tmp_path):
    registry = build_registry(VehicleDB(tmp_path / "vehicle.db"))
    registry["raises_value_error"] = _RaisesValueErrorTool()
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            response = c.post("/tools/execute", json={"name": "raises_value_error", "args": {}})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert response.json() == {"error": "tool 'raises_value_error' failed: boom"}


# ---------- ConfirmationGate (generic middleware, not yet wired to any tool) ----------


def test_confirmation_gate_needs_confirmation_on_first_call(tmp_path):
    db = VehicleDB(tmp_path / "vehicle.db")
    gated = ConfirmationGate(SetReminderTool(db))

    result = gated.execute(text="pick up dry cleaning")

    assert result == {"status": "needs_confirmation", "prompt": gated._prompt}
    # Nothing was actually saved yet.
    assert VehicleDB(tmp_path / "vehicle.db").list_reminders()["reminders"] == []


def test_confirmation_gate_performs_action_when_confirmed(tmp_path):
    db = VehicleDB(tmp_path / "vehicle.db")
    gated = ConfirmationGate(SetReminderTool(db))

    result = gated.execute(text="pick up dry cleaning", confirmed=True)

    assert result["status"] == "success"
    assert result["saved"] == "pick up dry cleaning"


def test_confirmation_gate_schema_adds_confirmed_param(tmp_path):
    db = VehicleDB(tmp_path / "vehicle.db")
    gated = ConfirmationGate(SetReminderTool(db))

    schema = gated.to_function_tool()

    assert "confirmed" in schema["parameters"]["properties"]
    assert schema["name"] == "set_reminder"


def test_registry_includes_agent_tools(client):
    names = {t["name"] for t in client.get("/tools/schemas").json()}
    assert {"start_research", "get_research_result"} <= names


def test_jobs_endpoints_roundtrip(client, monkeypatch):
    import time as _time

    import nova.server.tool_service as ts

    monkeypatch.setattr(ts, "_default_jobs", None)  # isolate from other tests
    jobs = ts.get_jobs()
    job_id = jobs.start("research", lambda query: {"status": "success", "answer": "hi"}, query="q")
    deadline = _time.monotonic() + 5
    while _time.monotonic() < deadline and jobs.get(job_id).status == "running":
        _time.sleep(0.02)
    listed = client.get("/jobs").json()
    assert any(j["id"] == job_id and j["status"] == "done" for j in listed)
    ann = client.get("/jobs/announcements").json()
    assert any(a["id"] == job_id and a["summary"] == "hi" for a in ann)
    assert all(a["id"] != job_id for a in client.get("/jobs/announcements").json())


def test_send_email_requires_two_step_confirmation(client):
    args = {"to": "a@b.c", "subject": "hi", "body": "hello"}
    first = client.post("/tools/execute", json={"name": "send_email", "args": args}).json()
    assert first["status"] == "needs_confirmation"
    second = client.post(
        "/tools/execute", json={"name": "send_email", "args": {**args, "confirmed": True}}
    ).json()
    assert second["status"] == "sent"
    assert second["to"] == "a@b.c"


def test_registry_contains_gated_send_payment(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVEAUTH_USE_MOCK", "1")
    registry = build_registry(
        VehicleDB(tmp_path / "vehicle.db"),
        driveauth_store=tmp_path / "driveauth_store",
    )
    tool = registry["send_payment"]
    # It is the gate, not the bare tool: schema advertises step_up_code.
    assert "step_up_code" in tool.parameters["properties"]
    out = tool.execute(payee="Chai Point", amount=50.0, beneficiary_known=True)
    assert out["status"] == "sent"
    assert out["auth"]["decision"] == "accept"


def test_pending_confirm_pins_on_route(client):
    """Server-side route must pick up pins from execute(needs_confirmation)."""
    _pending_confirm.clear()
    first = client.post(
        "/tools/execute",
        json={"name": "send_email", "args": {"to": "a@b.c", "subject": "s", "body": "b"}},
    ).json()
    assert first["status"] == "needs_confirmation"
    assert "send_email" in _pending_confirm
    body = client.post("/tools/route", json={"query": "Confirm.", "k": 8}).json()
    assert body["tool_choice"] == {"type": "function", "name": "send_email"}
    assert [t["name"] for t in body["tools"]] == ["send_email"]
    # Clear on successful confirmed execute
    client.post(
        "/tools/execute",
        json={
            "name": "send_email",
            "args": {"to": "a@b.c", "subject": "s", "body": "b", "confirmed": True},
        },
    )
    assert "send_email" not in _pending_confirm
    _pending_confirm.clear()


def test_pending_confirm_clears_on_topic_change(client):
    """Sticky step-up/confirm must not trap a later research/news turn (LFM live bug)."""
    _pending_confirm.clear()
    first = client.post(
        "/tools/execute",
        json={"name": "send_email", "args": {"to": "a@b.c", "subject": "s", "body": "b"}},
    ).json()
    assert first["status"] == "needs_confirmation"
    body = client.post(
        "/tools/route",
        json={"query": "can you do some research on inference engines", "k": 8},
    ).json()
    assert body.get("clear_pending") is True
    assert "send_email" not in _pending_confirm
    names = [t["name"] for t in body["tools"]]
    assert "send_email" not in names
    _pending_confirm.clear()


def test_create_drive_folder_requires_confirmation(client):
    first = client.post(
        "/tools/execute",
        json={"name": "create_drive_folder", "args": {"name": "Nova Demo"}},
    ).json()
    assert first["status"] == "needs_confirmation"
    assert "create_drive_folder" in _pending_confirm


def test_create_drive_folder_topic_change_cancels_pending(client):
    _pending_confirm.clear()
    client.post(
        "/tools/execute",
        json={"name": "create_drive_folder", "args": {"name": "Nova Demo"}},
    )
    assert "create_drive_folder" in _pending_confirm
    body = client.post(
        "/tools/route",
        json={"query": "what's the weather in Bangalore", "k": 8},
    ).json()
    assert body.get("clear_pending") is True
    assert "create_drive_folder" not in _pending_confirm
    _pending_confirm.clear()


def test_execute_preserves_drive_speak_filenames(tmp_path, monkeypatch):
    """Factual speak payload must keep exact Drive filenames (no paraphrase)."""
    from types import SimpleNamespace

    from nova.tools.mcp.drive import ListDriveFilesTool
    from nova.tools.vehicle import VehicleDB

    monkeypatch.setenv("DRIVEAUTH_USE_MOCK", "1")

    class _FakeMcp:
        def call_tool(self, name, arguments=None):
            assert name == "list_recent_files"
            return {
                "structuredContent": {
                    "files": [
                        {
                            "id": "a",
                            "title": "Q3 Budget.xlsx",
                            "mimeType": "application/vnd.ms-excel",
                        },
                        {
                            "id": "b",
                            "title": "Nova S notes.md",
                            "mimeType": "text/markdown",
                        },
                    ]
                }
            }

    registry = build_registry(VehicleDB(tmp_path / "vehicle.db"), driveauth_store=tmp_path / "da")
    registry["list_drive_files"] = ListDriveFilesTool(
        tokens=SimpleNamespace(
            configured=lambda: True,
            authenticated=lambda: True,
            get_access_token=lambda: "tok",
        ),
        mcp=_FakeMcp(),
    )
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        with TestClient(app) as c:
            out = c.post("/tools/execute", json={"name": "list_drive_files", "args": {}}).json()
    finally:
        app.dependency_overrides.clear()

    assert out["status"] == "success"
    assert out["speak"] == "Recent Drive files: Q3 Budget.xlsx; Nova S notes.md."
    assert out["files"][0]["name"] == "Q3 Budget.xlsx"

