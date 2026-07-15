"""Unit tests for Gmail + Drive MCP tools (mocked MCP caller)."""

from __future__ import annotations

from nova.tools.mcp.drive import CreateDriveFolderTool, ListDriveFilesTool
from nova.tools.mcp.gmail import CheckEmailTool


class _FakeTokens:
    def configured(self) -> bool:
        return True

    def authenticated(self) -> bool:
        return True

    def get_access_token(self) -> str:
        return "tok"


class _FakeMcp:
    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        fn = self.handlers.get(name)
        if fn is None:
            raise RuntimeError(f"unexpected tool {name}")
        return fn(arguments or {})


def test_check_email_summarize_includes_body():
    def search_threads(args):
        return {
            "structuredContent": {
                "threads": [
                    {
                        "id": "t1",
                        "messages": [
                            {
                                "id": "m1",
                                "sender": "SoundHound AI <news@example.com>",
                                "subject": "Leader report",
                                "snippet": "Gartner…",
                            }
                        ],
                    }
                ]
            }
        }

    def get_message(args):
        assert args.get("messageId") == "m1"
        return {
            "structuredContent": {
                "id": "m1",
                "sender": "SoundHound AI <news@example.com>",
                "subject": "Leader report",
                "plaintextBody": "Gartner named SoundHound a Leader. See the report.",
            }
        }

    mcp = _FakeMcp({"search_threads": search_threads, "get_message": get_message})
    tool = CheckEmailTool(tokens=_FakeTokens(), mcp=mcp)
    out = tool.execute(mode="summarize")
    assert out["status"] == "success"
    assert out["mode"] == "summarize"
    assert out["source"] == "google_gmail_mcp"
    assert "Gartner" in out["speak"]
    assert "Leader report" in out["speak"]
    assert "body" in (out.get("message") or {})


def test_check_email_latest_mode_lists_inbox():
    def search_threads(args):
        assert args.get("query") == "in:inbox"
        return {
            "structuredContent": {
                "threads": [
                    {
                        "messages": [
                            {
                                "id": "m9",
                                "sender": "Alex <alex@example.com>",
                                "subject": "Weekend plans",
                                "snippet": "Hi",
                            }
                        ]
                    }
                ]
            }
        }

    mcp = _FakeMcp({"search_threads": search_threads})
    tool = CheckEmailTool(tokens=_FakeTokens(), mcp=mcp)
    out = tool.execute(mode="latest")
    assert out["status"] == "success"
    assert out["mode"] == "latest"
    assert out["source"] == "google_gmail_mcp"
    assert "Weekend plans" in out["speak"]
    assert "Your latest email" in out["speak"]


def test_list_drive_files_speak_uses_exact_names():
    def list_recent_files(args):
        return {
            "structuredContent": {
                "files": [
                    {"id": "1", "title": "Q3 Budget.xlsx", "mimeType": "x"},
                    {"id": "2", "title": "Nova S notes.md", "mimeType": "y"},
                ]
            }
        }

    mcp = _FakeMcp({"list_recent_files": list_recent_files})
    tool = ListDriveFilesTool(tokens=_FakeTokens(), mcp=mcp)
    out = tool.execute()
    assert out["status"] == "success"
    assert out["speak"] == "Recent Drive files: Q3 Budget.xlsx; Nova S notes.md."
    assert [f["name"] for f in out["files"]] == ["Q3 Budget.xlsx", "Nova S notes.md"]


def test_create_drive_folder_via_mcp():
    def create_file(args):
        assert args["title"] == "Nova S"
        assert args["mimeType"] == "application/vnd.google-apps.folder"
        return {"structuredContent": {"id": "fld1", "title": args["title"]}}

    mcp = _FakeMcp({"create_file": create_file})
    tool = CreateDriveFolderTool(tokens=_FakeTokens(), mcp=mcp)
    out = tool.execute(name="Nova S")
    assert out["status"] == "success"
    assert out["source"] == "google_drive_mcp"
    assert out["speak"] == "Created Drive folder Nova S."
    assert out["folder"]["id"] == "fld1"
