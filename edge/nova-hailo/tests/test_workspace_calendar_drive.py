"""Unit tests for nova_hailo.connectors.workspace.{calendar,drive}.

Same shape as tests/test_workspace_gmail.py: most tests inject a fake MCP
caller (the `mcp` constructor parameter), which bypasses
nova_hailo.connectors.mcp_transport entirely. A few (marked below) instead
monkeypatch `mcp_transport.call_tool` itself, to exercise the real
(non-injected) path -- unwrap_call_tool_result -> unpack_mcp_result ->
the module's own parsing -- without ever opening a socket:
`mcp_transport.call_tool` is replaced before it would do any I/O, so
nothing beneath it (httpx, the session cache) ever runs.
"""
from __future__ import annotations

from nova_hailo.connectors import mcp_transport
from nova_hailo.connectors.workspace import calendar, drive
from nova_hailo.connectors.workspace.calendar import (
    CheckCalendarTool,
    CreateCalendarEventTool,
    DeleteCalendarEventTool,
)
from nova_hailo.connectors.workspace.drive import CreateDriveFolderTool, ListDriveFilesTool
from nova_hailo.edge_harness.policy import OEM_READONLY


class _FakeTokens:
    def __init__(self, *, configured=True, authenticated=True, access_token="tok"):
        self._configured = configured
        self._authenticated = authenticated
        self._access_token = access_token

    def configured(self) -> bool:
        return self._configured

    def authenticated(self) -> bool:
        return self._authenticated

    def get_access_token(self) -> str | None:
        return self._access_token


class _FakeMcp:
    def __init__(self, handlers: dict | None = None, error: Exception | None = None):
        self.handlers = handlers or {}
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        if self.error is not None:
            raise self.error
        fn = self.handlers.get(name)
        if fn is None:
            raise RuntimeError(f"unexpected tool {name}")
        return fn(arguments or {})


# —— CheckCalendarTool.execute, injected fake caller (no transport, no network) ——


def test_check_calendar_parses_recorded_list_events_payload():
    def list_events(args):
        assert "startTime" in args and "endTime" in args
        return {
            "structuredContent": {
                "events": [
                    {"id": "e1", "summary": "Standup", "start": "2026-07-15T09:00:00+00:00"},
                    {"id": "e2", "title": "Review", "startTime": "2026-07-16T10:30:00+00:00"},
                ]
            }
        }

    mcp = _FakeMcp({"list_events": list_events})
    tool = CheckCalendarTool(tokens=_FakeTokens(), mcp=mcp)
    out = tool.execute(day="week")

    assert out["status"] == "success"
    assert out["event_count"] == 2
    assert out["source"] == "google_calendar_mcp"
    assert out["events"] == [
        {"id": "e1", "title": "Standup", "start": "2026-07-15T09:00:00+00:00"},
        {"id": "e2", "title": "Review", "start": "2026-07-16T10:30:00+00:00"},
    ]
    assert "Standup" in out["speak"]
    assert mcp.calls[0][0] == "list_events"


def test_check_calendar_empty_week_speaks_sensibly_not_crash():
    def list_events(args):
        return {"structuredContent": {"events": []}}

    mcp = _FakeMcp({"list_events": list_events})
    tool = CheckCalendarTool(tokens=_FakeTokens(), mcp=mcp)
    out = tool.execute(day="week")

    assert out["status"] == "success"
    assert out["event_count"] == 0
    assert out["speak"] == "You have no upcoming events this week."


def test_check_calendar_mcp_error_surfaces_as_unavailable_payload_not_exception():
    mcp = _FakeMcp(error=RuntimeError("mcp_tool_error: insufficient scopes"))
    tool = CheckCalendarTool(tokens=_FakeTokens(), mcp=mcp)

    out = tool.execute(day="week")

    assert out["status"] == "unavailable"
    assert out["reason"] == "calendar_mcp_error"
    assert "insufficient scopes" in out["error"]


def test_check_calendar_oauth_not_ready_returns_unavailable_without_calling_mcp():
    mcp = _FakeMcp({"list_events": lambda args: {}})
    tool = CheckCalendarTool(tokens=_FakeTokens(authenticated=False), mcp=mcp)

    out = tool.execute(day="week")

    assert out["status"] == "unavailable"
    assert out["reason"] == "google_oauth_not_authenticated"
    assert mcp.calls == []


def test_check_calendar_timeout_passed_through_to_instance():
    tool = CheckCalendarTool(tokens=_FakeTokens(), timeout=3.5, mcp=_FakeMcp())
    assert tool._timeout == 3.5


# —— CheckCalendarTool, real (non-injected) path with a mocked transport ——


def test_check_calendar_real_path_end_to_end_with_mocked_transport(monkeypatch):
    seen = []

    def fake_call_tool(url, name, arguments=None, *, headers_fn=None, timeout=8.0):
        seen.append({"url": url, "name": name, "arguments": arguments, "timeout": timeout})
        assert headers_fn() == {"Authorization": "Bearer tok"}
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "structuredContent": {
                    "events": [
                        {"id": "e1", "summary": "1:1", "start": "2026-07-15T09:00:00+00:00"},
                    ]
                }
            },
        }

    monkeypatch.setattr(mcp_transport, "call_tool", fake_call_tool)

    tool = CheckCalendarTool(tokens=_FakeTokens(access_token="tok"), timeout=6.0)
    out = tool.execute(day="week")

    assert out["status"] == "success"
    assert out["events"] == [
        {"id": "e1", "title": "1:1", "start": "2026-07-15T09:00:00+00:00"}
    ]
    assert seen[0]["url"] == calendar.CALENDAR_MCP_URL
    assert seen[0]["name"] == "list_events"
    assert seen[0]["timeout"] == 6.0


def test_check_calendar_real_path_malformed_envelope_reports_unavailable(monkeypatch):
    def fake_call_tool(url, name, arguments=None, *, headers_fn=None, timeout=8.0):
        return {"jsonrpc": "2.0", "id": 1}  # no "result" key -- not a valid envelope

    monkeypatch.setattr(mcp_transport, "call_tool", fake_call_tool)

    tool = CheckCalendarTool(tokens=_FakeTokens())
    out = tool.execute(day="week")

    assert out["status"] == "unavailable"
    assert out["reason"] == "calendar_mcp_error"


# —— ListDriveFilesTool.execute, injected fake caller (no transport, no network) ——


def test_list_drive_files_parses_recorded_list_recent_files_payload():
    def list_recent_files(args):
        assert args == {"pageSize": 8, "orderBy": "lastModified"}
        return {
            "structuredContent": {
                "files": [
                    {"id": "f1", "title": "Budget.xlsx", "mimeType": "x", "modifiedTime": "t1"},
                    {"id": "f2", "name": "Notes.doc", "mime_type": "y", "modified_time": "t2"},
                ]
            }
        }

    mcp = _FakeMcp({"list_recent_files": list_recent_files})
    tool = ListDriveFilesTool(tokens=_FakeTokens(), mcp=mcp)
    out = tool.execute(query="")

    assert out["status"] == "success"
    assert out["source"] == "google_drive_mcp"
    assert out["file_count"] == 2
    assert out["files"] == [
        {"id": "f1", "name": "Budget.xlsx", "mime_type": "x", "modified": "t1"},
        {"id": "f2", "name": "Notes.doc", "mime_type": "y", "modified": "t2"},
    ]
    assert "Budget.xlsx" in out["speak"]
    assert mcp.calls == [("list_recent_files", {"pageSize": 8, "orderBy": "lastModified"})]


def test_list_drive_files_query_uses_search_files():
    def search_files(args):
        assert args == {"query": "title contains 'budget'", "pageSize": 8}
        return {"structuredContent": {"files": []}}

    mcp = _FakeMcp({"search_files": search_files})
    tool = ListDriveFilesTool(tokens=_FakeTokens(), mcp=mcp)
    out = tool.execute(query="budget")

    assert out["status"] == "success"
    assert out["file_count"] == 0
    assert "No Drive files matching budget" in out["speak"]
    assert mcp.calls == [("search_files", {"query": "title contains 'budget'", "pageSize": 8})]


def test_list_drive_files_mcp_error_surfaces_as_unavailable_payload_not_exception():
    mcp = _FakeMcp(error=RuntimeError("mcp_tool_error: insufficient scopes"))
    tool = ListDriveFilesTool(tokens=_FakeTokens(), mcp=mcp)

    out = tool.execute(query="")

    assert out["status"] == "unavailable"
    assert out["reason"] == "drive_mcp_error"
    assert "insufficient scopes" in out["error"]
    assert out["speak"] == "Google Drive is unavailable. Reconnect Google in Settings."


def test_list_drive_files_oauth_not_ready_returns_unavailable_without_calling_mcp():
    mcp = _FakeMcp({"list_recent_files": lambda args: {}})
    tool = ListDriveFilesTool(tokens=_FakeTokens(authenticated=False), mcp=mcp)

    out = tool.execute(query="")

    assert out["status"] == "unavailable"
    assert out["reason"] == "google_oauth_not_authenticated"
    assert mcp.calls == []


def test_list_drive_files_timeout_passed_through_to_instance():
    tool = ListDriveFilesTool(tokens=_FakeTokens(), timeout=4.25, mcp=_FakeMcp())
    assert tool._timeout == 4.25


# —— ListDriveFilesTool, real (non-injected) path with a mocked transport ——


def test_list_drive_files_real_path_end_to_end_with_mocked_transport(monkeypatch):
    seen = []

    def fake_call_tool(url, name, arguments=None, *, headers_fn=None, timeout=8.0):
        seen.append({"url": url, "name": name, "arguments": arguments, "timeout": timeout})
        assert headers_fn() == {"Authorization": "Bearer tok"}
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "structuredContent": {
                    "files": [{"id": "f1", "title": "Report.pdf", "mimeType": "z"}]
                }
            },
        }

    monkeypatch.setattr(mcp_transport, "call_tool", fake_call_tool)

    tool = ListDriveFilesTool(tokens=_FakeTokens(access_token="tok"), timeout=6.0)
    out = tool.execute(query="")

    assert out["status"] == "success"
    assert out["files"] == [
        {"id": "f1", "name": "Report.pdf", "mime_type": "z", "modified": ""}
    ]
    assert seen[0]["url"] == drive.DRIVE_MCP_URL
    assert seen[0]["name"] == "list_recent_files"
    assert seen[0]["timeout"] == 6.0


def test_list_drive_files_real_path_malformed_envelope_reports_unavailable(monkeypatch):
    def fake_call_tool(url, name, arguments=None, *, headers_fn=None, timeout=8.0):
        return {"jsonrpc": "2.0", "id": 1}  # no "result" key -- not a valid envelope

    monkeypatch.setattr(mcp_transport, "call_tool", fake_call_tool)

    tool = ListDriveFilesTool(tokens=_FakeTokens())
    out = tool.execute(query="")

    assert out["status"] == "unavailable"
    assert out["reason"] == "drive_mcp_error"
    assert out["speak"] == "Google Drive is unavailable. Reconnect Google in Settings."


# —— Write tools are ported for parity but must stay unreachable ——
#
# tools.write_enabled is false and policy.OEM_READONLY lists only read
# tools, so these classes existing is fine -- them appearing in
# OEM_READONLY (or any other allowlist) is the bug this guards against.


def test_write_tools_exist_but_are_absent_from_oem_readonly():
    assert CreateCalendarEventTool is not None
    assert DeleteCalendarEventTool is not None
    assert CreateDriveFolderTool is not None

    write_tool_names = {
        CreateCalendarEventTool.name,
        DeleteCalendarEventTool.name,
        CreateDriveFolderTool.name,
    }
    assert write_tool_names == {
        "create_calendar_event",
        "delete_calendar_event",
        "create_drive_folder",
    }
    assert write_tool_names.isdisjoint(OEM_READONLY)
