"""Unit tests for LFM2.5-230M tool agent + model route mode."""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from nova.server.tool_service import app, get_registry, build_registry
from nova.tools.vehicle import VehicleDB


def _client(tmp_path, monkeypatch, *, route_mode: str = "model"):
    monkeypatch.setenv("NOVA_TOOL_ROUTE_MODE", route_mode)
    monkeypatch.setenv("DRIVEAUTH_USE_MOCK", "1")
    monkeypatch.setenv("NOVA_ROUTER_LLM_URL", "http://127.0.0.1:8081/v1")
    reg = build_registry(VehicleDB(tmp_path / "v.db"), driveauth_store=tmp_path / "da")
    app.dependency_overrides[get_registry] = lambda: reg
    return TestClient(app), reg


def test_route_model_mode_strips_toolbox(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch, route_mode="model")
    r = c.post(
        "/tools/route",
        json={"query": "Hey, what is the stock price of Amazon today.", "k": 8},
    )
    body = r.json()
    assert body["tool_choice"] == "none"
    assert len(body["tools"]) >= 1
    assert len(body["tools"]) < 10


def test_agent_low_signal_skips_router(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    with patch("nova.server.tool_agent._call_router_llm") as mock_llm:
        r = c.post("/tools/agent", json={"query": "Hey there.", "execute": True})
        body = r.json()
        assert body["skipped"] == "low_signal"
        assert body["tool_choice"] == "none"
        mock_llm.assert_not_called()


def test_agent_executes_router_tool_call(tmp_path, monkeypatch):
    c, reg = _client(tmp_path, monkeypatch)

    def fake_router(_registry, query):
        return (
            [{"id": "c1", "name": "web_search", "args": {"query": query}}],
            "",
        )

    fake_result = {"speak": "Amazon is trading near one eighty.", "ok": True}
    with (
        patch("nova.server.tool_agent._call_router_llm", side_effect=fake_router),
        patch.object(reg["web_search"], "execute", return_value=fake_result) as ex,
    ):
        r = c.post(
            "/tools/agent",
            json={"query": "stock price of Amazon today", "execute": True},
        )
    body = r.json()
    assert body["needs_tools"] is True
    assert body["tool_calls"][0]["name"] == "web_search"
    assert "Amazon" in body["speak_payload"]
    assert "verbatim" in body["speak_instructions"].lower()
    assert body["tool_choice"] == "none"
    ex.assert_called_once()


def test_agent_no_tool_text(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)

    def fake_router(_registry, _query):
        return [], "NO_TOOL"

    with patch("nova.server.tool_agent._call_router_llm", side_effect=fake_router):
        r = c.post("/tools/agent", json={"query": "tell me a joke", "execute": True})
    body = r.json()
    assert body["needs_tools"] is False
    assert body["skipped"] == "no_tool"
    assert body["tool_choice"] == "none"


def test_parse_tool_calls_dict_shape():
    from nova.server.tool_agent import _parse_tool_calls

    msg = {
        "tool_calls": [
            {
                "id": "x",
                "function": {"name": "check_calendar", "arguments": '{"day":"today"}'},
            }
        ]
    }
    calls = _parse_tool_calls(msg)
    assert calls == [{"id": "x", "name": "check_calendar", "args": {"day": "today"}}]


def test_forced_web_search_stock_and_news():
    from nova.server.tool_agent import _forced_web_search, _correct_calls

    stock = _forced_web_search("What is the current stock price of Amazon.")
    assert stock is not None
    assert "Amazon" in stock["query"]

    news = _forced_web_search("Hey, can you tell me the current news of Bangalore today.")
    assert news is not None
    assert news.get("place") == "Bangalore"

    assert _forced_web_search("What's the weather in Bangalore") is None

    fixed = _correct_calls(
        "stock price of Amazon",
        [{"id": "1", "name": "get_weather", "args": {"place": "Amazon"}}],
    )
    assert fixed[0]["name"] == "web_search"


def test_correct_calls_fills_email_mode_latest_and_summarize():
    from nova.server.tool_agent import _correct_calls

    latest = _correct_calls(
        "what's the latest email that I have",
        [{"id": "1", "name": "check_email", "args": {}}],
    )
    assert latest[0]["args"]["mode"] == "latest"

    summarized = _correct_calls(
        "Summarize that email.",
        [{"id": "1", "name": "check_email", "args": {"mode": "unread"}}],
    )
    assert summarized[0]["args"]["mode"] == "summarize"


def test_agent_articulator_returns_zero_tools(tmp_path, monkeypatch):
    c, reg = _client(tmp_path, monkeypatch)
    fake_result = {"speak": "Amazon near one eighty.", "ok": True, "status": "success"}
    with (
        patch("nova.server.tool_agent._call_router_llm") as mock_llm,
        patch.object(reg["web_search"], "execute", return_value=fake_result),
    ):
        r = c.post(
            "/tools/agent",
            json={"query": "What is the current stock price of Amazon.", "execute": True},
        )
    body = r.json()
    assert body["tool_choice"] == "none"
    assert body["tools"] == []
    mock_llm.assert_not_called()


def test_agent_unsupported_drive_delete_capability_error(tmp_path, monkeypatch):
    from nova.server.session_state import reset_sessions

    reset_sessions()
    c, _ = _client(tmp_path, monkeypatch)
    with patch("nova.server.tool_agent._call_router_llm") as mock_llm:
        r = c.post(
            "/tools/agent",
            json={
                "query": "Delete that Drive file named budget.",
                "execute": True,
                "session_id": "sess-del",
            },
        )
    body = r.json()
    mock_llm.assert_not_called()
    assert body["tool_choice"] == "none"
    assert body["tools"] == []
    assert body["results"][0]["result"]["status"] == "unsupported"
    assert body["results"][0]["result"]["capability"] == "delete_drive_file"
    assert "can't delete" in body["speak_payload"].lower()
    assert "deleted" not in body["speak_payload"].lower()


def test_agent_followup_third_file_uses_session_state(tmp_path, monkeypatch):
    from nova.server.session_state import get_session, reset_sessions

    reset_sessions()
    scratch = get_session("sess-drive")
    scratch.last_drive_files = [
        {"id": "1", "name": "Notes.txt"},
        {"id": "2", "name": "Invoice.pdf"},
        {"id": "3", "name": "Q3 Budget.xlsx"},
    ]
    c, _ = _client(tmp_path, monkeypatch)
    with patch("nova.server.tool_agent._call_router_llm") as mock_llm:
        r = c.post(
            "/tools/agent",
            json={
                "query": "Open the third file.",
                "execute": True,
                "session_id": "sess-drive",
            },
        )
    body = r.json()
    mock_llm.assert_not_called()
    assert "Q3 Budget.xlsx" in body["speak_payload"]
    assert body["tools"] == []


def test_agent_draft_reply_grounds_from_last_email(tmp_path, monkeypatch):
    from nova.server.session_state import get_session, reset_sessions

    reset_sessions()
    scratch = get_session("sess-mail")
    scratch.last_emails = [
        {
            "id": "m1",
            "from": "Priya Sharma <priya@example.com>",
            "subject": "Q3 roadmap review",
            "preview": "Sounds good",
        }
    ]
    c, _ = _client(tmp_path, monkeypatch)

    def fake_router(_registry, _query):
        return (
            [{"id": "c1", "name": "send_email", "args": {}}],
            "",
        )

    with patch("nova.server.tool_agent._call_router_llm", side_effect=fake_router):
        r = c.post(
            "/tools/agent",
            json={
                "query": "Reply to that email saying thanks.",
                "execute": True,
                "session_id": "sess-mail",
            },
        )
    body = r.json()
    call = body["tool_calls"][0]
    assert call["name"] == "send_email"
    assert call["args"]["to"] == "priya@example.com"
    assert call["args"]["subject"].startswith("Re:")
    assert "thanks" in call["args"]["body"].lower()
    # ConfirmationGate: first execute needs confirmation, not a fabricated send.
    assert body["results"][0]["result"]["status"] == "needs_confirmation"
    assert scratch.draft_reply is not None
    assert scratch.draft_reply["to"] == "priya@example.com"


def test_agent_forces_web_search_without_router(tmp_path, monkeypatch):
    c, reg = _client(tmp_path, monkeypatch)
    fake_result = {"speak": "Amazon near one eighty.", "ok": True}
    with (
        patch("nova.server.tool_agent._call_router_llm") as mock_llm,
        patch.object(reg["web_search"], "execute", return_value=fake_result) as ex,
    ):
        r = c.post(
            "/tools/agent",
            json={"query": "What is the current stock price of Amazon.", "execute": True},
        )
    body = r.json()
    assert body["needs_tools"] is True
    assert body["tool_calls"][0]["name"] == "web_search"
    mock_llm.assert_not_called()
    ex.assert_called_once()


def test_forced_calendar_not_reminders():
    from nova.server.tool_agent import _forced_calendar, _correct_calls

    assert _forced_calendar("Hey, check my calendar.") == {"day": "week"}
    assert _forced_calendar("check my calendar for tomorrow") == {"day": "tomorrow"}
    assert _forced_calendar("Hey, Jackman can refer tomorrow.") == {"day": "tomorrow"}
    assert _forced_calendar("what are my reminders") is None
    fixed = _correct_calls(
        "Hey, check my calendar.",
        [{"id": "1", "name": "list_reminders", "args": {}}],
    )
    assert fixed[0]["name"] == "check_calendar"


def test_forced_email_stt_truncation():
    from nova.server.routing import _normalize_stt_query
    from nova.server.tool_agent import _forced_email, _correct_calls

    assert "email" in _normalize_stt_query("Hey, can you check my E.").lower()
    assert _forced_email("Hey, check my E.") == {"mode": "latest"}
    fixed = _correct_calls(
        "Hey, check my E.",
        [{"id": "1", "name": "get_weather", "args": {"place": "New York"}}],
    )
    assert fixed[0]["name"] == "check_email"
