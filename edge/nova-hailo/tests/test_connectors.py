"""User-addable connector registry: validation, caps, enable/disable, and
settings round-trip -- including the backwards-compatibility case where an
existing hmi_settings.json has no "connectors" key at all.

Connector tools are NOT wired into openai_tools_for_profile yet (deliberately
out of scope) -- these tests only cover the registry/persistence/API layer.
"""
from __future__ import annotations

import ipaddress
import json
import socket

import pytest

from nova_hailo.connectors import mcp_client, registry, url_safety
from nova_hailo.settings_store import load_settings, save_settings

_REAL_GETADDRINFO = socket.getaddrinfo
_PUBLIC_TEST_IP = "93.184.216.34"  # ordinary public unicast address, not example.com's actual IP


def _offline_getaddrinfo(host, *args, **kwargs):
    """Deterministic, network-free stand-in for socket.getaddrinfo.

    A literal IP (or "localhost") is resolved for real -- that needs no
    network and is exactly what the SSRF-reject tests below depend on.
    Any other symbolic hostname (e.g. "example.com") gets a canned public
    IP instead of an actual DNS query, so the whole suite stays offline and
    deterministic per the security review's requirement.
    """
    try:
        ipaddress.ip_address(host)
        return _REAL_GETADDRINFO(host, *args, **kwargs)
    except ValueError:
        pass
    if host == "localhost":
        return _REAL_GETADDRINFO(host, *args, **kwargs)
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_TEST_IP, 0))]


@pytest.fixture(autouse=True)
def _offline_dns(monkeypatch):
    """Every test in this module runs against a fake resolver -- registry
    and mcp_client both call the real SSRF guard on every add_connector/
    list_tools/call_tool, and this repo's test policy is no real network
    calls from the suite."""
    monkeypatch.setattr(url_safety.socket, "getaddrinfo", _offline_getaddrinfo)


@pytest.fixture()
def settings_path(tmp_path):
    return tmp_path / "hmi_settings.json"


# —— settings_store round-trip / backwards compatibility ——


def test_load_settings_defaults_connectors_to_empty_list(settings_path):
    data = load_settings(settings_path)
    assert data["connectors"] == []


def test_old_settings_file_without_connectors_key_still_loads(settings_path):
    """A settings file written before this feature existed must load fine."""
    settings_path.write_text(
        json.dumps({"mode": "openrouter", "local_hef": "qwen2", "or_model": "deepseek/deepseek-v4-flash-0731"})
    )
    data = load_settings(settings_path)
    assert data["connectors"] == []
    assert data["mode"] == "openrouter"
    assert data["local_hef"] == "qwen2"
    assert data["or_model"] == "deepseek/deepseek-v4-flash-0731"


def test_save_settings_does_not_disturb_mode_or_model(settings_path):
    save_settings({"mode": "openrouter", "or_model": "deepseek/deepseek-v3.2"}, settings_path)
    registry.add_connector("My Notes", "https://example.com/mcp", path=settings_path)
    data = load_settings(settings_path)
    assert data["mode"] == "openrouter"
    assert data["or_model"] == "deepseek/deepseek-v3.2"
    assert len(data["connectors"]) == 1


def test_connectors_round_trip_through_disk(settings_path):
    added = registry.add_connector("My Notes", "https://example.com/mcp", path=settings_path)
    reloaded = load_settings(settings_path)
    assert reloaded["connectors"] == [added]


def test_malformed_connector_entries_are_dropped_not_fatal(settings_path):
    settings_path.write_text(
        json.dumps(
            {
                "mode": "local",
                "local_hef": "qwen2",
                "or_model": "x",
                "connectors": [
                    {"id": "cx_aaaaaaaa", "url": "https://ok.example/mcp", "label": "Ok"},
                    {"id": "", "url": "https://bad.example/mcp"},  # missing id
                    {"url": "https://also-bad.example/mcp"},  # missing id entirely
                    "not-a-dict",
                    {"id": "cx_bbbbbbbb"},  # missing url
                ],
            }
        )
    )
    data = load_settings(settings_path)
    assert len(data["connectors"]) == 1
    assert data["connectors"][0]["id"] == "cx_aaaaaaaa"


# —— add_connector validation ——


def test_add_connector_defaults_to_disabled(settings_path):
    c = registry.add_connector("My Notes", "https://example.com/mcp", path=settings_path)
    assert c["enabled"] is False
    assert c["tools"] == []
    assert c["kind"] == "mcp_http"
    assert registry.is_valid_id(c["id"])


def test_add_connector_rejects_empty_label(settings_path):
    with pytest.raises(registry.ConnectorError):
        registry.add_connector("   ", "https://example.com/mcp", path=settings_path)


@pytest.mark.parametrize("bad_url", ["ftp://example.com/mcp", "not-a-url", "javascript:alert(1)", ""])
def test_add_connector_rejects_non_http_scheme(settings_path, bad_url):
    with pytest.raises(registry.ConnectorError):
        registry.add_connector("Label", bad_url, path=settings_path)


def test_add_connector_rejects_url_without_host(settings_path):
    with pytest.raises(registry.ConnectorError):
        registry.add_connector("Label", "http://", path=settings_path)


def test_add_connector_accepts_https(settings_path):
    c = registry.add_connector("Label", "https://example.com/mcp", path=settings_path)
    assert c["url"] == "https://example.com/mcp"


def test_add_connector_accepts_http(settings_path):
    # NOT localhost -- an http(s) connector pointed at a loopback/private
    # address is exactly what the SSRF guard exists to reject (see the SSRF
    # guard tests below). This only exercises the "http scheme is allowed"
    # branch, against the offline fixture's canned public address.
    c = registry.add_connector("Label", "http://myserver.example/mcp", path=settings_path)
    assert c["url"] == "http://myserver.example/mcp"


def test_add_connector_rejects_duplicate_url(settings_path):
    registry.add_connector("First", "https://example.com/mcp", path=settings_path)
    with pytest.raises(registry.ConnectorError):
        registry.add_connector("Second", "https://example.com/mcp", path=settings_path)


def test_add_connector_rejects_duplicate_url_trailing_slash_and_case(settings_path):
    registry.add_connector("First", "https://Example.com/mcp/", path=settings_path)
    with pytest.raises(registry.ConnectorError):
        registry.add_connector("Second", "https://example.com/mcp", path=settings_path)


def test_add_connector_rejects_unsupported_kind(settings_path):
    with pytest.raises(registry.ConnectorError):
        registry.add_connector("Label", "https://example.com/mcp", kind="mcp_stdio", path=settings_path)


def test_add_connector_ids_are_unique(settings_path):
    ids = set()
    for i in range(5):
        c = registry.add_connector(f"Label {i}", f"https://example.com/mcp{i}", path=settings_path)
        ids.add(c["id"])
    assert len(ids) == 5


# —— SSRF guard (url_safety.assert_public_http_url) ——
#
# _refresh_tools_best_effort in app.py fires an outbound MCP request the
# moment a connector is CREATED, with no auth on any route and uvicorn bound
# to 0.0.0.0 -- so any device that can reach the Pi can otherwise make it
# fetch an arbitrary internal address. These tests are the security
# boundary; be thorough, not just spec-literal.


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1:8000",
        "http://localhost/mcp",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
        "http://10.0.0.5/mcp",
        "http://192.168.1.35/mcp",
        "http://172.16.0.1/mcp",
        "http://[::1]/mcp",
        "http://0.0.0.0/",
        "file:///etc/passwd",
        "gopher://x/",
    ],
)
def test_ssrf_guard_rejects_unsafe_urls(bad_url):
    with pytest.raises(url_safety.UnsafeConnectorURLError):
        url_safety.assert_public_http_url(bad_url)


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1:8000",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/mcp",
    ],
)
def test_add_connector_rejects_unsafe_urls(settings_path, bad_url):
    """The same guard applies at registration time via registry.add_connector,
    raising the registry's own error type so the API returns a clean 400."""
    with pytest.raises(registry.ConnectorError):
        registry.add_connector("Bad", bad_url, path=settings_path)


def test_ssrf_guard_rejects_ipv4_mapped_ipv6_loopback():
    """::ffff:127.0.0.1 is IPv6 syntax wrapping an IPv4 loopback address --
    must not slip past the guard just because it isn't literally "127.0.0.1"."""
    with pytest.raises(url_safety.UnsafeConnectorURLError):
        url_safety.assert_public_http_url("http://[::ffff:127.0.0.1]/mcp")


def test_ssrf_guard_rejects_unresolvable_host(monkeypatch):
    def resolve_fail(host, *a, **kw):
        raise socket.gaierror("nodename nor servname provided, or not known")

    monkeypatch.setattr(url_safety.socket, "getaddrinfo", resolve_fail)
    with pytest.raises(url_safety.UnsafeConnectorURLError):
        url_safety.assert_public_http_url("https://nonexistent.invalid/mcp")


def test_ssrf_guard_accepts_public_url(monkeypatch):
    """A normal public https URL is accepted -- resolver is explicitly
    monkeypatched here (on top of the autouse fixture) so this never makes a
    real network call, matching the security review's requirement."""

    def resolve_public(host, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(url_safety.socket, "getaddrinfo", resolve_public)
    out = url_safety.assert_public_http_url("https://example.com/mcp")
    assert out == "https://example.com/mcp"


def test_fetch_time_recheck_blocks_dns_rebinding(settings_path, monkeypatch):
    """Registration-time validation alone is bypassable by DNS rebinding: a
    hostname resolves to a public address when the connector is added and to
    a loopback address by the time a tool call actually goes out. The fetch-
    time re-check inside mcp_client._rpc_call must catch this and the
    outbound httpx call must never happen."""

    def resolve_public(host, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(url_safety.socket, "getaddrinfo", resolve_public)
    c = registry.add_connector("Rebinder", "https://rebind.example/mcp", path=settings_path)

    def resolve_private(host, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(url_safety.socket, "getaddrinfo", resolve_private)

    calls = {"n": 0}

    def fake_post(*a, **kw):
        calls["n"] += 1
        raise AssertionError("must never reach the network after rebinding")

    monkeypatch.setattr(mcp_client.httpx, "post", fake_post)

    with pytest.raises(mcp_client.MCPClientError):
        mcp_client.list_tools(c["url"])
    assert calls["n"] == 0


# —— hard caps ——


def test_max_connectors_cap(settings_path):
    for i in range(registry.MAX_CONNECTORS):
        registry.add_connector(f"Label {i}", f"https://example.com/mcp{i}", path=settings_path)
    with pytest.raises(registry.ConnectorError):
        registry.add_connector("One too many", "https://example.com/overflow", path=settings_path)
    assert len(registry.list_connectors(settings_path)) == registry.MAX_CONNECTORS


def test_enable_rejected_when_it_would_exceed_total_tool_cap(settings_path):
    c1 = registry.add_connector("Big", "https://example.com/big", path=settings_path)
    registry.set_tools(
        c1["id"],
        [{"name": f"tool_{i}"} for i in range(registry.MAX_TOTAL_TOOLS + 1)],
        path=settings_path,
    )
    with pytest.raises(registry.ConnectorError):
        registry.enable_connector(c1["id"], path=settings_path)
    # Rejected enable must not have flipped the flag.
    assert registry.list_connectors(settings_path)[0]["enabled"] is False


def test_enable_allowed_exactly_at_total_tool_cap(settings_path):
    c1 = registry.add_connector("Exact", "https://example.com/exact", path=settings_path)
    registry.set_tools(
        c1["id"],
        [{"name": f"tool_{i}"} for i in range(registry.MAX_TOTAL_TOOLS)],
        path=settings_path,
    )
    enabled = registry.enable_connector(c1["id"], path=settings_path)
    assert enabled["enabled"] is True
    assert registry.total_enabled_tools(settings_path) == registry.MAX_TOTAL_TOOLS


def test_enable_rejected_when_combined_with_other_enabled_connector_exceeds_cap(settings_path):
    half = registry.MAX_TOTAL_TOOLS // 2
    c1 = registry.add_connector("A", "https://example.com/a", path=settings_path)
    c2 = registry.add_connector("B", "https://example.com/b", path=settings_path)
    registry.set_tools(c1["id"], [{"name": f"a{i}"} for i in range(half + 5)], path=settings_path)
    registry.set_tools(c2["id"], [{"name": f"b{i}"} for i in range(half + 5)], path=settings_path)
    registry.enable_connector(c1["id"], path=settings_path)
    with pytest.raises(registry.ConnectorError):
        registry.enable_connector(c2["id"], path=settings_path)


def test_tools_growing_past_cap_after_enable_auto_disables(settings_path):
    """Regression for a cap bypass: enable_connector's old early-return for
    an already-enabled connector skipped the cap check entirely, and
    app.py's enable handler refreshes tools (via set_tools) BEFORE calling
    enable_connector -- so a connector's own server growing its advertised
    tool count after being enabled could silently push total_enabled_tools()
    past MAX_TOTAL_TOOLS with enabled:true persisted. set_tools must itself
    be cap-aware for an already-enabled connector."""
    c = registry.add_connector("Grower", "https://example.com/grower", path=settings_path)
    registry.set_tools(c["id"], [{"name": f"t{i}"} for i in range(5)], path=settings_path)
    registry.enable_connector(c["id"], path=settings_path)
    assert registry.total_enabled_tools(settings_path) == 5

    # The connector's server now advertises far more tools -- simulates the
    # refresh app.py's enable handler performs before re-checking the cap.
    grown_count = registry.MAX_TOTAL_TOOLS + 1
    updated = registry.set_tools(
        c["id"], [{"name": f"t{i}"} for i in range(grown_count)], path=settings_path
    )
    assert updated["enabled"] is False  # auto-disabled by the cap-aware guard
    assert registry.total_enabled_tools(settings_path) == 0
    assert registry.list_connectors(settings_path)[0]["enabled"] is False


def test_reenable_after_growth_past_cap_is_refused(settings_path):
    """Second line of defense: even if a caller tried to force it back on,
    enable_connector's own cap check (which now runs unconditionally,
    not just for a False->True transition) must refuse."""
    c = registry.add_connector("Grower", "https://example.com/grower2", path=settings_path)
    registry.set_tools(c["id"], [{"name": f"t{i}"} for i in range(5)], path=settings_path)
    registry.enable_connector(c["id"], path=settings_path)

    grown_count = registry.MAX_TOTAL_TOOLS + 1
    registry.set_tools(c["id"], [{"name": f"t{i}"} for i in range(grown_count)], path=settings_path)
    # set_tools already auto-disabled it; re-enabling explicitly must still
    # be refused rather than silently exceeding the cap.
    with pytest.raises(registry.ConnectorError):
        registry.enable_connector(c["id"], path=settings_path)
    assert registry.list_connectors(settings_path)[0]["enabled"] is False
    assert registry.total_enabled_tools(settings_path) == 0


def test_tools_growing_within_cap_stays_enabled(settings_path):
    """The cap-aware guard in set_tools must not be trigger-happy -- growth
    that stays within the cap must not flip an enabled connector off."""
    c = registry.add_connector("Grower", "https://example.com/grower3", path=settings_path)
    registry.set_tools(c["id"], [{"name": "t1"}], path=settings_path)
    registry.enable_connector(c["id"], path=settings_path)

    updated = registry.set_tools(
        c["id"], [{"name": f"t{i}"} for i in range(registry.MAX_TOTAL_TOOLS)], path=settings_path
    )
    assert updated["enabled"] is True
    assert registry.total_enabled_tools(settings_path) == registry.MAX_TOTAL_TOOLS


# —— enable / disable / remove ——


def test_enable_then_disable_round_trip(settings_path):
    c = registry.add_connector("Label", "https://example.com/mcp", path=settings_path)
    enabled = registry.enable_connector(c["id"], path=settings_path)
    assert enabled["enabled"] is True
    disabled = registry.disable_connector(c["id"], path=settings_path)
    assert disabled["enabled"] is False


def test_enable_is_idempotent(settings_path):
    c = registry.add_connector("Label", "https://example.com/mcp", path=settings_path)
    registry.enable_connector(c["id"], path=settings_path)
    again = registry.enable_connector(c["id"], path=settings_path)
    assert again["enabled"] is True


def test_enable_unknown_id_raises_not_found(settings_path):
    with pytest.raises(registry.ConnectorNotFoundError):
        registry.enable_connector("cx_deadbeef", path=settings_path)


def test_disable_unknown_id_raises_not_found(settings_path):
    with pytest.raises(registry.ConnectorNotFoundError):
        registry.disable_connector("cx_deadbeef", path=settings_path)


def test_malformed_id_is_rejected_as_not_found(settings_path):
    registry.add_connector("Label", "https://example.com/mcp", path=settings_path)
    with pytest.raises(registry.ConnectorNotFoundError):
        registry.enable_connector("../../etc/passwd", path=settings_path)


def test_remove_connector(settings_path):
    c = registry.add_connector("Label", "https://example.com/mcp", path=settings_path)
    assert registry.remove_connector(c["id"], path=settings_path) is True
    assert registry.list_connectors(settings_path) == []


def test_remove_unknown_connector_returns_false(settings_path):
    assert registry.remove_connector("cx_deadbeef", path=settings_path) is False


def test_removing_an_enabled_connector_frees_its_tool_budget(settings_path):
    c = registry.add_connector("Label", "https://example.com/mcp", path=settings_path)
    registry.set_tools(c["id"], [{"name": "t1"}, {"name": "t2"}], path=settings_path)
    registry.enable_connector(c["id"], path=settings_path)
    assert registry.total_enabled_tools(settings_path) == 2
    registry.remove_connector(c["id"], path=settings_path)
    assert registry.total_enabled_tools(settings_path) == 0


def test_is_valid_id():
    assert registry.is_valid_id("cx_ab12cd34")
    assert not registry.is_valid_id("cx_short")
    assert not registry.is_valid_id("not-cx-prefixed-12345678")
    assert not registry.is_valid_id("")
    assert not registry.is_valid_id(None)


# —— mcp_client: minimal HTTP transport ——


class _FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("bad status", request=None, response=self)

    def json(self):
        return self._body


def test_mcp_client_list_tools_happy_path(monkeypatch):
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        seen["timeout"] = timeout
        return _FakeResponse(
            {"jsonrpc": "2.0", "id": json["id"], "result": {"tools": [{"name": "search"}, {"name": "lookup"}]}}
        )

    monkeypatch.setattr(mcp_client.httpx, "post", fake_post)
    tools = mcp_client.list_tools("https://example.com/mcp")
    assert [t["name"] for t in tools] == ["search", "lookup"]
    assert seen["url"] == "https://example.com/mcp"
    assert seen["json"]["method"] == "tools/list"
    assert seen["timeout"] == mcp_client.DEFAULT_TIMEOUT


def test_mcp_client_list_tools_ignores_malformed_entries(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        return _FakeResponse(
            {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "ok"}, {"no_name": True}, "not-a-dict"]}}
        )

    monkeypatch.setattr(mcp_client.httpx, "post", fake_post)
    tools = mcp_client.list_tools("https://example.com/mcp")
    assert [t["name"] for t in tools] == ["ok"]


def test_mcp_client_raises_on_jsonrpc_error(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        return _FakeResponse({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no such method"}})

    monkeypatch.setattr(mcp_client.httpx, "post", fake_post)
    with pytest.raises(mcp_client.MCPClientError):
        mcp_client.list_tools("https://example.com/mcp")


def test_mcp_client_raises_on_transport_error(monkeypatch):
    import httpx

    def fake_post(url, json=None, timeout=None):
        raise httpx.ConnectError("connection refused", request=None)

    monkeypatch.setattr(mcp_client.httpx, "post", fake_post)
    with pytest.raises(mcp_client.MCPClientError):
        mcp_client.list_tools("https://example.com/mcp")


def test_mcp_client_call_tool_sends_name_and_arguments(monkeypatch):
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["json"] = json
        seen["timeout"] = timeout
        return _FakeResponse({"jsonrpc": "2.0", "id": json["id"], "result": {"ok": True}})

    monkeypatch.setattr(mcp_client.httpx, "post", fake_post)
    result = mcp_client.call_tool("https://example.com/mcp", "search", {"query": "cats"})
    assert result == {"ok": True}
    assert seen["json"]["method"] == "tools/call"
    assert seen["json"]["params"] == {"name": "search", "arguments": {"query": "cats"}}
    assert seen["timeout"] == mcp_client.CALL_TIMEOUT
