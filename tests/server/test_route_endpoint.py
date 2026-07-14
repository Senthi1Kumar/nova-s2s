from fastapi.testclient import TestClient
from nova.server.tool_service import app, get_registry, build_registry
from nova.tools.vehicle import VehicleDB


def _client(tmp_path, monkeypatch, *, route_mode: str = "force"):
    # Tests expect force semantics unless a case opts into full.
    monkeypatch.setenv("NOVA_TOOL_ROUTE_MODE", route_mode)
    monkeypatch.setenv("DRIVEAUTH_USE_MOCK", "1")
    reg = build_registry(VehicleDB(tmp_path / "v.db"), driveauth_store=tmp_path / "da")
    app.dependency_overrides[get_registry] = lambda: reg
    return TestClient(app)


def test_route_returns_topk_with_relevant_tool_first(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "set the driver temperature to 22", "k": 5})
    assert r.status_code == 200
    body = r.json()
    names = [t["name"] for t in body["tools"]]
    assert names == ["set_hvac"]
    assert body["tool_choice"] == "required"
    assert body["top_score"] >= 2.0


def test_route_full_mode_all_tools_auto(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, route_mode="full")
    r = c.post(
        "/tools/route",
        json={"query": "Hey, what is the stock price of Amazon today.", "k": 8},
    )
    body = r.json()
    names = [t["name"] for t in body["tools"]]
    assert "web_search" in names
    assert "check_calendar" in names
    assert len(names) > 8
    assert body["tool_choice"] == "auto"
    assert body["tool_choice"] != {"type": "function", "name": "check_calendar"}


def test_route_honors_pinned(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    # Non-confirm pins still stick; confirm tools are cleared on topic change.
    r = c.post("/tools/route", json={"query": "hello", "k": 3, "pinned": ["web_search"]})
    body = r.json()
    assert [t["name"] for t in body["tools"]] == ["web_search"]
    assert body["tool_choice"] == "required"


def test_route_clears_confirm_pin_on_topic_change(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "hello", "k": 3, "pinned": ["send_payment"]})
    body = r.json()
    assert body.get("clear_pending") is True
    assert body["tool_choice"] != {"type": "function", "name": "send_payment"}
    assert "send_payment" not in [t["name"] for t in body["tools"]]


def test_route_email_forces_check_email(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "Check my emails.", "k": 8})
    body = r.json()
    assert [t["name"] for t in body["tools"]] == ["check_email"]
    assert body["tool_choice"] == {"type": "function", "name": "check_email"}


def test_route_tomorrow_forces_check_calendar_not_mock(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "What about tomorrow.", "k": 8})
    body = r.json()
    assert body["tool_choice"] == {"type": "function", "name": "check_calendar"}
    assert [t["name"] for t in body["tools"]] == ["check_calendar"]


def test_route_today_only_forces_check_calendar(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "Hey, check my calendar for today day only.", "k": 8})
    body = r.json()
    assert body["tool_choice"] == {"type": "function", "name": "check_calendar"}


def test_route_weather_forces_get_weather(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "Can you check the weather in Bangalore.", "k": 8})
    body = r.json()
    assert body["tools"][0]["name"] == "get_weather"
    assert body["tool_choice"] == {"type": "function", "name": "get_weather"}


def test_route_cabin_status_forces_query_vehicle_status(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post(
        "/tools/route",
        json={"query": "Now tell me all the three zones, cabin temperature.", "k": 8},
    )
    body = r.json()
    assert body["tools"][0]["name"] == "query_vehicle_status"
    assert body["tool_choice"] == {"type": "function", "name": "query_vehicle_status"}


def test_route_chitchat_blocks_tools(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "Hey there.", "k": 8})
    body = r.json()
    assert body["tool_choice"] == "none"


def test_route_stock_forces_web_search(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post(
        "/tools/route",
        json={"query": "Okay, so now tell me the current stock price of Amazon.", "k": 8},
    )
    body = r.json()
    assert body["tools"][0]["name"] == "web_search"
    assert body["tool_choice"] == {"type": "function", "name": "web_search"}


def test_route_news_forces_web_search_not_weather(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "What's the current news today in Bangalore.", "k": 8})
    body = r.json()
    assert body["tools"][0]["name"] == "web_search"
    assert body["tool_choice"] == {"type": "function", "name": "web_search"}


def test_route_news_today_not_calendar(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    for q in (
        "What is the current news today in Bangalore.",
        "Hey, I want you to search for the current news today in Bangalore.",
        "Current news today in Bangalore.",
    ):
        r = c.post("/tools/route", json={"query": q, "k": 8})
        body = r.json()
        assert body["tool_choice"] == {"type": "function", "name": "web_search"}, q


def test_route_check_my_e_today_is_email(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "Check my E today.", "k": 8})
    body = r.json()
    assert body["tool_choice"] == {"type": "function", "name": "check_email"}


def test_route_calendarra_tomorrow_is_calendar(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "Check my calendarra for tomorrowrow.", "k": 8})
    body = r.json()
    assert body["tool_choice"] == {"type": "function", "name": "check_calendar"}


def test_route_create_drive_folder(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post(
        "/tools/route",
        json={"query": "Can you create a directory in drive named as Nova S.", "k": 8},
    )
    body = r.json()
    assert body["tools"][0]["name"] == "create_drive_folder"
    assert body["tool_choice"] == {"type": "function", "name": "create_drive_folder"}


def test_route_check_windows_queries_status(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "Hey, check the windows.", "k": 8})
    body = r.json()
    assert body["tool_choice"] == {"type": "function", "name": "query_vehicle_status"}


def test_route_close_windows_sets_windows(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "Then, close the windows.", "k": 8})
    body = r.json()
    assert body["tool_choice"] == {"type": "function", "name": "set_windows"}


def test_route_bare_check_amazon_does_not_force_email(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "Check for Amazon.", "k": 8})
    body = r.json()
    assert body["tool_choice"] != {"type": "function", "name": "check_email"}


def test_route_confirm_only_without_pin_is_none(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "Confirm.", "k": 8})
    body = r.json()
    assert body["tool_choice"] == "none"
    assert body["tools"]  # non-empty so UI session.update won't stick tools:[]


def test_route_yeahyeah_is_none(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "YeahYeah.", "k": 8})
    body = r.json()
    assert body["tool_choice"] == "none"
    assert body["tools"]


def test_route_he_there_is_none(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "He there.", "k": 8})
    body = r.json()
    assert body["tool_choice"] == "none"


def test_route_confirm_with_pin_forces_payment(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post(
        "/tools/route",
        json={"query": "Confirm.", "k": 8, "pinned": ["send_payment"]},
    )
    body = r.json()
    assert [t["name"] for t in body["tools"]] == ["send_payment"]
    assert body["tool_choice"] == {"type": "function", "name": "send_payment"}


def test_route_delete_reminder_requires_reminder_tools(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/tools/route", json={"query": "Can you delete the reminder.", "k": 8})
    body = r.json()
    names = [t["name"] for t in body["tools"]]
    assert set(names) <= {"list_reminders", "set_reminder"}
    assert body["tool_choice"] == "required"
    assert body["tool_choice"] != {"type": "function", "name": "set_reminder"}
