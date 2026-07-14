"""Unit tests for Gmail summarize + Drive create folder (mocked HTTP).

Callers: pytest. Exercises nova.tools.mcp.gmail / drive used by tool_service.
No production data files — synthetic Gmail/Drive JSON only.
User: live session — summarize unread mail; create Drive folder Nova S.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

from nova.tools.mcp.drive import CreateDriveFolderTool
from nova.tools.mcp.gmail import CheckEmailTool


class _FakeTokens:
    def configured(self) -> bool:
        return True

    def authenticated(self) -> bool:
        return True

    def get_access_token(self) -> str:
        return "tok"


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_check_email_summarize_includes_body():
    def fake_get(url, params=None, headers=None, timeout=None):
        if "messages" in url and url.rstrip("/").endswith("messages"):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"messages": [{"id": "m1"}]},
            )
        body = _b64("Gartner named SoundHound a Leader. See the report.")
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "id": "m1",
                "snippet": "Gartner…",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "SoundHound AI <news@example.com>"},
                        {"name": "Subject", "value": "Leader report"},
                    ],
                    "mimeType": "text/plain",
                    "body": {"data": body},
                },
            },
        )

    tool = CheckEmailTool(tokens=_FakeTokens(), http_get=fake_get)
    out = tool.execute(mode="summarize")
    assert out["status"] == "success"
    assert out["mode"] == "summarize"
    assert "Gartner" in out["speak"]
    assert "Leader report" in out["speak"]
    assert "body" in (out.get("message") or {})


def test_check_email_latest_mode_lists_inbox():
    calls: list[dict] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": dict(params or {})})
        if url.rstrip("/").endswith("messages"):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"messages": [{"id": "m9"}]},
            )
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "id": "m9",
                "snippet": "Hi",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Alex <alex@example.com>"},
                        {"name": "Subject", "value": "Weekend plans"},
                    ]
                },
            },
        )

    tool = CheckEmailTool(tokens=_FakeTokens(), http_get=fake_get)
    out = tool.execute(mode="latest")
    assert out["status"] == "success"
    assert out["mode"] == "latest"
    assert calls[0]["params"].get("q") == "in:inbox"
    assert "Weekend plans" in out["speak"]
    assert "Your latest email" in out["speak"]


def test_list_drive_files_speak_uses_exact_names():
    from nova.tools.mcp.drive import ListDriveFilesTool

    def fake_get(url, params=None, headers=None, timeout=None):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "files": [
                    {"id": "1", "name": "Q3 Budget.xlsx", "mimeType": "x"},
                    {"id": "2", "name": "Nova S notes.md", "mimeType": "y"},
                ]
            },
        )

    tool = ListDriveFilesTool(tokens=_FakeTokens(), http_get=fake_get)
    out = tool.execute()
    assert out["status"] == "success"
    assert out["speak"] == "Recent Drive files: Q3 Budget.xlsx; Nova S notes.md."
    assert [f["name"] for f in out["files"]] == ["Q3 Budget.xlsx", "Nova S notes.md"]


def test_create_drive_folder_posts_mime():
    posted: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        posted["json"] = json
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"id": "fld1", "name": json["name"]},
        )

    tool = CreateDriveFolderTool(tokens=_FakeTokens(), http_post=fake_post)
    out = tool.execute(name="Nova S")
    assert out["status"] == "success"
    assert out["speak"] == "Created Drive folder Nova S."
    assert posted["json"]["mimeType"] == "application/vnd.google-apps.folder"
    assert posted["json"]["name"] == "Nova S"
