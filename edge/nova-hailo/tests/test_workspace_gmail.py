"""Unit tests for nova_hailo.connectors.workspace.{runtime,gmail}.

Most tests inject a fake MCP caller (the `mcp` constructor parameter), which
bypasses nova_hailo.connectors.mcp_transport entirely. A few (marked below)
instead monkeypatch `mcp_transport.call_tool` itself, to exercise
CheckEmailTool's real (non-injected) path -- the unwrap_call_tool_result ->
unpack_mcp_result -> _threads_to_messages composition -- without ever
opening a socket: `mcp_transport.call_tool` is replaced before it would do
any I/O, so nothing beneath it (httpx, the session cache) ever runs.
"""
from __future__ import annotations

import pytest

from nova_hailo.connectors import mcp_transport
from nova_hailo.connectors.workspace import gmail
from nova_hailo.connectors.workspace.gmail import CheckEmailTool
from nova_hailo.connectors.workspace.runtime import (
    oauth_ready_or_error,
    unpack_mcp_result,
    unwrap_call_tool_result,
)


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


# —— oauth_ready_or_error ——


def test_oauth_ready_or_error_not_configured():
    err = oauth_ready_or_error(_FakeTokens(configured=False))
    assert err == {
        "status": "unavailable",
        "reason": "google_oauth_not_configured",
        "hint": "Set GOOGLE_OAUTH_CLIENT_ID/SECRET in .env, then connect Google in Settings.",
    }


def test_oauth_ready_or_error_none_tokens_is_not_configured():
    err = oauth_ready_or_error(None)
    assert err is not None
    assert err["reason"] == "google_oauth_not_configured"


def test_oauth_ready_or_error_not_authenticated():
    err = oauth_ready_or_error(_FakeTokens(authenticated=False))
    assert err == {
        "status": "unavailable",
        "reason": "google_oauth_not_authenticated",
        "hint": "Open Settings → Connect with Google (work Chrome profile).",
    }


def test_oauth_ready_or_error_token_refresh_failed():
    err = oauth_ready_or_error(_FakeTokens(access_token=None))
    assert err == {
        "status": "unavailable",
        "reason": "google_oauth_token_refresh_failed",
        "hint": "Reconnect Google in Settings.",
    }


def test_oauth_ready_or_error_none_when_ready():
    assert oauth_ready_or_error(_FakeTokens()) is None


# —— CheckEmailTool.execute, injected fake caller (no transport, no network) ——


def test_execute_unread_parses_recorded_search_threads_payload():
    def search_threads(args):
        assert args == {"query": "is:unread", "pageSize": 5}
        return {
            "structuredContent": {
                "threads": [
                    {
                        "id": "t1",
                        "messages": [
                            {
                                "id": "m1",
                                "from": "Alice <alice@example.com>",
                                "subject": "Hello",
                                "snippet": "Hi there",
                            }
                        ],
                    },
                    {
                        "id": "t2",
                        "messages": [
                            {
                                "id": "m2",
                                "sender": "Bob <bob@example.com>",
                                "subject": "Re: Project",
                                "snippet": "Sounds good",
                            }
                        ],
                    },
                ]
            }
        }

    mcp = _FakeMcp({"search_threads": search_threads})
    tool = CheckEmailTool(tokens=_FakeTokens(), mcp=mcp)
    out = tool.execute(mode="unread")

    assert out["status"] == "success"
    assert out["mode"] == "unread"
    assert out["source"] == "google_gmail_mcp"
    assert out["message_count"] == 2
    assert out["messages"] == [
        {"id": "m1", "from": "Alice <alice@example.com>", "subject": "Hello", "preview": "Hi there"},
        {"id": "m2", "from": "Bob <bob@example.com>", "subject": "Re: Project", "preview": "Sounds good"},
    ]
    assert out["speak"] == "You have 2 unread emails. Alice: Hello; Bob: Re: Project."
    assert mcp.calls == [("search_threads", {"query": "is:unread", "pageSize": 5})]


def test_execute_empty_inbox_speaks_sensibly_not_crash():
    def search_threads(args):
        return {"structuredContent": {"threads": []}}

    mcp = _FakeMcp({"search_threads": search_threads})
    tool = CheckEmailTool(tokens=_FakeTokens(), mcp=mcp)
    out = tool.execute(mode="unread")

    assert out["status"] == "success"
    assert out["message_count"] == 0
    assert out["messages"] == []
    assert out["speak"] == "You have no unread emails."


def test_execute_latest_empty_inbox_speaks_sensibly():
    def search_threads(args):
        assert args == {"query": "in:inbox", "pageSize": 3}
        return {"structuredContent": {"threads": []}}

    mcp = _FakeMcp({"search_threads": search_threads})
    tool = CheckEmailTool(tokens=_FakeTokens(), mcp=mcp)
    out = tool.execute(mode="latest")

    assert out["status"] == "success"
    assert out["speak"] == "Your inbox has no recent emails."


def test_execute_mcp_error_surfaces_as_unavailable_payload_not_exception():
    mcp = _FakeMcp(error=RuntimeError("mcp_tool_error: insufficient scopes"))
    tool = CheckEmailTool(tokens=_FakeTokens(), mcp=mcp)

    out = tool.execute(mode="unread")

    assert out["status"] == "unavailable"
    assert out["reason"] == "gmail_mcp_error"
    assert "insufficient scopes" in out["error"]
    assert out["speak"] == "Gmail is unavailable. Reconnect Google in Settings."


def test_execute_oauth_not_ready_returns_unavailable_without_calling_mcp():
    mcp = _FakeMcp({"search_threads": lambda args: {}})
    tool = CheckEmailTool(tokens=_FakeTokens(authenticated=False), mcp=mcp)

    out = tool.execute(mode="unread")

    assert out["status"] == "unavailable"
    assert out["reason"] == "google_oauth_not_authenticated"
    assert out["speak"] == "Gmail is unavailable. Reconnect Google in Settings."
    assert mcp.calls == []


def test_execute_summarize_includes_body():
    def search_threads(args):
        return {
            "structuredContent": {
                "threads": [
                    {
                        "id": "t1",
                        "messages": [
                            {
                                "id": "m1",
                                "sender": "News <news@example.com>",
                                "subject": "Leader report",
                                "snippet": "short",
                            }
                        ],
                    }
                ]
            }
        }

    def get_message(args):
        assert args == {"messageId": "m1", "messageFormat": "FULL_CONTENT"}
        return {
            "structuredContent": {
                "id": "m1",
                "sender": "News <news@example.com>",
                "subject": "Leader report",
                "plaintextBody": "Full body text about the report.",
            }
        }

    mcp = _FakeMcp({"search_threads": search_threads, "get_message": get_message})
    tool = CheckEmailTool(tokens=_FakeTokens(), mcp=mcp)
    out = tool.execute(mode="summarize")

    assert out["status"] == "success"
    assert out["mode"] == "summarize"
    assert "Leader report" in out["speak"]
    assert "Full body text about the report." in out["speak"]
    assert out["message"]["body"] == "Full body text about the report."


# —— unwrap_call_tool_result, in isolation ——
#
# The highest-risk function in this port: it is the one thing that did not
# exist in the code being ported, bridging mcp_transport.call_tool's full
# JSON-RPC envelope onto unpack_mcp_result's "bare result object" contract.
# A malformed envelope must raise, not degrade to a value that reads as a
# genuinely empty result -- see the runtime.py module docstring.


def test_unwrap_call_tool_result_well_formed_envelope():
    envelope = {"jsonrpc": "2.0", "id": 3, "result": {"threads": []}}
    assert unwrap_call_tool_result(envelope) == {"threads": []}


def test_unwrap_call_tool_result_missing_result_key_raises():
    with pytest.raises(mcp_transport.McpError):
        unwrap_call_tool_result({"jsonrpc": "2.0", "id": 3})


@pytest.mark.parametrize("bad_envelope", ["not-an-envelope", None, [], 42])
def test_unwrap_call_tool_result_non_dict_envelope_raises(bad_envelope):
    with pytest.raises(mcp_transport.McpError):
        unwrap_call_tool_result(bad_envelope)


def test_unwrap_call_tool_result_non_dict_result_is_preserved_not_dropped():
    """A `result` that is present but not a dict is a server reply in an
    unexpected shape, not a missing/garbage envelope -- it must reach
    unpack_mcp_result's own {"value": ...} fallback intact rather than
    being coerced to {} and silently discarded."""
    envelope = {"jsonrpc": "2.0", "id": 3, "result": "unexpected-string-result"}
    assert unwrap_call_tool_result(envelope) == "unexpected-string-result"


def test_unwrap_then_unpack_non_dict_result_becomes_value_dict():
    """Composition check for the case above: unpack_mcp_result's existing
    fallback is what actually makes the non-dict result diagnosable."""
    envelope = {"jsonrpc": "2.0", "id": 3, "result": "weird"}
    raw = unwrap_call_tool_result(envelope)
    assert unpack_mcp_result(raw) == {"value": "weird"}


def test_unwrap_call_tool_result_null_result_raises():
    """`{"result": null}` is valid JSON-RPC shape -- `"result" in envelope`
    is True for it -- but it is exactly as unusable as a missing `result`
    key: unwrapping it to `None` feeds unpack_mcp_result's `not isinstance`
    branch, which yields `{"value": None}`, which every caller's message/
    event/file parser reads as zero items -- a false "empty" result, not a
    genuinely empty one. This must raise, same as the missing-key case
    above."""
    with pytest.raises(mcp_transport.McpError):
        unwrap_call_tool_result({"jsonrpc": "2.0", "id": 3, "result": None})


# —— CheckEmailTool, real (non-injected) path with a mocked transport ——
#
# These do not pass `mcp=`, so CheckEmailTool._call() takes its real branch
# and calls mcp_transport.call_tool -- which is monkeypatched here, so no
# httpx.Client, no session cache, and no socket are ever touched. This is
# what actually exercises unwrap_call_tool_result -> unpack_mcp_result ->
# _threads_to_messages together, rather than reasoning about them in
# isolation.


def test_execute_real_path_end_to_end_with_mocked_transport(monkeypatch):
    seen = []

    def fake_call_tool(url, name, arguments=None, *, headers_fn=None, timeout=8.0):
        seen.append({"url": url, "name": name, "arguments": arguments, "timeout": timeout})
        assert headers_fn() == {"Authorization": "Bearer tok"}
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "structuredContent": {
                    "threads": [
                        {
                            "id": "t1",
                            "messages": [
                                {
                                    "id": "m1",
                                    "from": "Carol <carol@example.com>",
                                    "subject": "Ping",
                                    "snippet": "pong",
                                }
                            ],
                        }
                    ]
                }
            },
        }

    monkeypatch.setattr(mcp_transport, "call_tool", fake_call_tool)

    tool = CheckEmailTool(tokens=_FakeTokens(access_token="tok"), timeout=6.0)
    out = tool.execute(mode="unread")

    assert out["status"] == "success"
    assert out["messages"] == [
        {"id": "m1", "from": "Carol <carol@example.com>", "subject": "Ping", "preview": "pong"}
    ]
    assert out["speak"] == "You have 1 unread emails. Carol: Ping."
    assert seen == [
        {
            "url": gmail.GMAIL_MCP_URL,
            "name": "search_threads",
            "arguments": {"query": "is:unread", "pageSize": 5},
            "timeout": 6.0,
        }
    ]


def test_execute_real_path_genuinely_empty_inbox_still_reports_empty(monkeypatch):
    def fake_call_tool(url, name, arguments=None, *, headers_fn=None, timeout=8.0):
        return {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"threads": []}}}

    monkeypatch.setattr(mcp_transport, "call_tool", fake_call_tool)

    tool = CheckEmailTool(tokens=_FakeTokens())
    out = tool.execute(mode="unread")

    assert out["status"] == "success"
    assert out["message_count"] == 0
    assert out["speak"] == "You have no unread emails."


def test_execute_real_path_malformed_envelope_reports_unavailable_not_empty_inbox(monkeypatch):
    """The ruling this test enforces: a malformed envelope (no `result`
    field at all here) must surface as "Gmail is unavailable", never as a
    fabricated "you have no unread emails" -- distinct from a genuinely
    empty inbox, covered above, which must keep reporting empty."""

    def fake_call_tool(url, name, arguments=None, *, headers_fn=None, timeout=8.0):
        return {"jsonrpc": "2.0", "id": 1}  # no "result" key -- not a valid envelope

    monkeypatch.setattr(mcp_transport, "call_tool", fake_call_tool)

    tool = CheckEmailTool(tokens=_FakeTokens())
    out = tool.execute(mode="unread")

    assert out["status"] == "unavailable"
    assert out["reason"] == "gmail_mcp_error"
    assert out["speak"] == "Gmail is unavailable. Reconnect Google in Settings."


def test_execute_real_path_null_result_reports_unavailable_not_empty_inbox(monkeypatch):
    """The false-empty-inbox bug this fix closes: `{"result": null}` used to
    unwrap to `None`, which unpack_mcp_result turned into `{"value": None}`,
    which read as zero threads -- reporting "You have no unread emails." as
    fact when the transport had actually failed. It must now surface as
    unavailable, exactly like the missing-`result`-key case above."""

    def fake_call_tool(url, name, arguments=None, *, headers_fn=None, timeout=8.0):
        return {"jsonrpc": "2.0", "id": 1, "result": None}

    monkeypatch.setattr(mcp_transport, "call_tool", fake_call_tool)

    tool = CheckEmailTool(tokens=_FakeTokens())
    out = tool.execute(mode="unread")

    assert out["status"] == "unavailable"
    assert out["reason"] == "gmail_mcp_error"
    assert out["speak"] == "Gmail is unavailable. Reconnect Google in Settings."


def test_execute_real_path_non_dict_result_reports_empty_via_value_fallback(monkeypatch):
    """The milder, deliberately-preserved case at execute() level (a prior
    review flagged this as having only composition-level coverage, i.e. only
    the isolated unwrap_call_tool_result/unpack_mcp_result checks above): a
    present, non-None, non-dict `result` is a server reply in an unexpected
    shape, not an invalid envelope. It must reach unpack_mcp_result's
    {"value": ...} fallback and read as harmlessly empty -- not raise, and
    not be confused with the null/missing-result cases, which must raise."""

    def fake_call_tool(url, name, arguments=None, *, headers_fn=None, timeout=8.0):
        return {"jsonrpc": "2.0", "id": 1, "result": "unexpected-string-result"}

    monkeypatch.setattr(mcp_transport, "call_tool", fake_call_tool)

    tool = CheckEmailTool(tokens=_FakeTokens())
    out = tool.execute(mode="unread")

    assert out["status"] == "success"
    assert out["message_count"] == 0
    assert out["speak"] == "You have no unread emails."
