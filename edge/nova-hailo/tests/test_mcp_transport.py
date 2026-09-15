"""Tests for the cached MCP transport (nova_hailo/connectors/mcp_transport.py).

All network access is faked via `httpx.MockTransport` -- no request ever
leaves the process. Each test asserts on the number and shape of requests
the fake server observed, since the whole point of this module is to make
the handshake happen at most once (and its expiry retry happen at most
once).
"""
from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from nova_hailo.connectors import mcp_transport

URL = "https://mcp.example.test/mcp/v1"

# Captured before any test monkeypatches mcp_transport.httpx.Client, so the
# fake client factory below always builds a *real* httpx.Client (just with a
# mock transport swapped in) instead of recursing into itself.
_REAL_CLIENT_CLS = httpx.Client


@pytest.fixture(autouse=True)
def _clean_module_state():
    mcp_transport.reset()
    yield
    mcp_transport.reset()


@pytest.fixture
def install_transport(monkeypatch):
    """Route every `httpx.Client()` the module creates through `handler`."""

    def _install(handler):
        mock_transport = httpx.MockTransport(handler)

        def fake_client(*args, **kwargs):
            kwargs["transport"] = mock_transport
            return _REAL_CLIENT_CLS(*args, **kwargs)

        monkeypatch.setattr(mcp_transport.httpx, "Client", fake_client)

    return _install


def _rpc_body(request: httpx.Request) -> dict:
    return json.loads(request.content or b"{}")


def _ok(body: dict, result: object = None, session_id: str | None = None) -> httpx.Response:
    headers = {"Mcp-Session-Id": session_id} if session_id else None
    result = {} if result is None else result
    payload = {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
    return httpx.Response(200, json=payload, headers=headers)


def test_one_initialize_across_many_calls(install_transport):
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _rpc_body(request)
        seen_methods.append(body.get("method"))
        if body.get("method") == "initialize":
            return _ok(body, session_id="sess-1")
        if body.get("method") == "notifications/initialized":
            return _ok(body)
        return _ok(body, result={"ok": True})

    install_transport(handler)

    for _ in range(5):
        result = mcp_transport.call(URL, "tools/list")
        assert result["result"] == {"ok": True}

    assert seen_methods.count("initialize") == 1


def test_expired_session_retries_exactly_once(install_transport):
    state = {"initialize": 0, "tool_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = _rpc_body(request)
        method = body.get("method")
        if method == "initialize":
            state["initialize"] += 1
            return _ok(body, session_id=f"sess-{state['initialize']}")
        if method == "notifications/initialized":
            return _ok(body)
        if method == "tools/call":
            state["tool_calls"] += 1
            if state["tool_calls"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "error": {"code": -32000, "message": "Session sess-1 not found"},
                    },
                )
            return _ok(body, result={"content": [], "isError": False})
        raise AssertionError(f"unexpected method: {method}")

    install_transport(handler)

    result = mcp_transport.call_tool(URL, "list_events", {})

    assert result["result"] == {"content": [], "isError": False}
    assert state["initialize"] == 2  # one re-init, not a loop
    assert state["tool_calls"] == 2  # one retry, not a loop


def test_http_500_fails_fast_without_retry(install_transport):
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _rpc_body(request)
        seen_methods.append(body.get("method"))
        if body.get("method") == "initialize":
            return _ok(body, session_id="sess-1")
        if body.get("method") == "notifications/initialized":
            return _ok(body)
        return httpx.Response(500, text="Internal Server Error")

    install_transport(handler)

    with pytest.raises(mcp_transport.McpError):
        mcp_transport.call(URL, "tools/list")

    # Exactly one attempt at the failing call -- a real outage must not be
    # doubled by a retry loop.
    assert seen_methods.count("tools/list") == 1


def test_connection_error_fails_fast_without_retry(install_transport):
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _rpc_body(request)
        seen_methods.append(body.get("method"))
        if body.get("method") == "initialize":
            return _ok(body, session_id="sess-1")
        if body.get("method") == "notifications/initialized":
            return _ok(body)
        raise httpx.ConnectTimeout("connect timed out", request=request)

    install_transport(handler)

    with pytest.raises(httpx.ConnectTimeout):
        mcp_transport.call(URL, "tools/list")

    assert seen_methods.count("tools/list") == 1


def test_concurrent_calls_create_one_session(install_transport):
    init_count = 0
    count_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal init_count
        body = _rpc_body(request)
        if body.get("method") == "initialize":
            with count_lock:
                init_count += 1
            time.sleep(0.05)  # widen the window for a would-be race
            return _ok(body, session_id="sess-1")
        if body.get("method") == "notifications/initialized":
            return _ok(body)
        return _ok(body, result={"ok": True})

    install_transport(handler)

    results: list[dict] = []
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait(timeout=5)
            results.append(mcp_transport.call(URL, "tools/list"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    assert len(results) == 2
    assert len(mcp_transport._sessions) == 1
    assert init_count == 1


def test_concurrent_rpc_calls_overlap_after_warm_init(install_transport):
    """Steady-state calls must not queue behind each other.

    Regression test: `call()` used to hold `session.lock` across the whole
    RPC, so two concurrent calls to an already-initialized session were
    fully serialized even though `httpx.Client` supports concurrent use.
    Both worker requests must be in flight *at the same time* for the
    shared barrier below to trip; if the lock still wrapped the RPC, the
    second request would never even be sent until the first fully
    returned, and the first would time out waiting alone at the barrier.
    """
    barrier = threading.Barrier(2, timeout=2)
    barrier_armed = {"on": False}
    entered: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _rpc_body(request)
        method = body.get("method")
        if method == "initialize":
            return _ok(body, session_id="sess-1")
        if method == "notifications/initialized":
            return _ok(body)
        if method == "tools/list":
            if barrier_armed["on"]:
                entered.append(time.monotonic())
                barrier.wait()  # only trips if both requests are in flight
            return _ok(body, result={"ok": True})
        raise AssertionError(f"unexpected method: {method}")

    install_transport(handler)

    mcp_transport.call(URL, "tools/list")  # warm the session, barrier off
    barrier_armed["on"] = True

    results: list[dict] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(mcp_transport.call(URL, "tools/list"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, errors
    assert len(results) == 2
    assert len(entered) == 2


def test_replace_session_reuses_sibling_instead_of_closing_it(install_transport):
    """`_replace_session` must evict by identity, not just by URL.

    Direct, deterministic regression test for the exact contract the fix
    depends on: once one caller has swapped in a replacement for `stale`, a
    second caller that also observed `stale` (because it read the cache
    before the swap) must reuse that same replacement instead of evicting
    it -- it is no longer the "current" session, so it must not be torn
    down just because a caller is still holding a reference to it.
    """
    install_transport(lambda request: _ok(_rpc_body(request)))

    stale = mcp_transport._get_session(URL)

    first = mcp_transport._replace_session(URL, stale)
    assert mcp_transport._sessions[URL] is first
    assert stale.client.is_closed

    # A second caller that captured `stale` before the first swap retries
    # with the same (now stale-to-it-too) reference.
    second = mcp_transport._replace_session(URL, stale)

    assert second is first
    assert not first.client.is_closed
    assert mcp_transport._sessions[URL] is first


def test_concurrent_expiry_never_closes_a_sibling_replacement(install_transport, monkeypatch):
    """Concurrent expiry must converge on one replacement, not fight over it.

    Regression test: the old retry path discarded/evicted "whatever is
    cached for this URL" rather than the exact stale object a caller
    observed. Under concurrent expiry (several callers sharing one now-dead
    session), a slower thread's discard could close a faster sibling's
    brand-new, already-initialized replacement -- possibly while that
    sibling's own retry request was still in flight on it.

    Both worker threads below share one pre-expired session and hit expiry
    together via real requests. Getting the harmful interleaving to
    reproduce deterministically (rather than depending on how the OS
    scheduler happens to interleave two CPU-bound threads under the GIL)
    takes two rendezvous points, not one: an `expiry_barrier` inside the
    fake server's "session expired" response makes both threads' *original*
    requests genuinely overlap (without it, one thread's request can race
    all the way through discard-and-replace before the other has even sent
    its request, so it never observes the same stale session at all); then
    `ordered_replace_session` ensures whichever thread reaches the retry's
    replace step first has fully finished replacing -- and started using
    the replacement -- before the second thread is let in, so the second
    thread's replace call is guaranteed to run while the first is genuinely
    using the new session. That combination is exactly the interleaving the
    bug depended on.
    """
    start_barrier = threading.Barrier(2, timeout=2)
    expiry_barrier = threading.Barrier(2, timeout=2)
    state = {"initialize": 0}
    init_lock = threading.Lock()
    real_replace_session = mcp_transport._replace_session
    order_lock = threading.Lock()
    first_claimed = {"done": False}
    first_replaced = threading.Event()

    def ordered_replace_session(url: str, stale):
        with order_lock:
            is_first = not first_claimed["done"]
            first_claimed["done"] = True
        if is_first:
            try:
                return real_replace_session(url, stale)
            finally:
                first_replaced.set()
        # Second arrival: wait until the first caller's replacement is
        # installed (and, per the sleep below, already in use) before
        # running the same code path it would run in a genuine race.
        assert first_replaced.wait(timeout=2)
        return real_replace_session(url, stale)

    monkeypatch.setattr(mcp_transport, "_replace_session", ordered_replace_session)

    def handler(request: httpx.Request) -> httpx.Response:
        body = _rpc_body(request)
        method = body.get("method")
        if method == "initialize":
            with init_lock:
                state["initialize"] += 1
                n = state["initialize"]
            if n > 1:
                # Give the first thread's replacement a real window of use
                # before the second thread (already unblocked, above) runs
                # its own replace call -- this is the window a buggy
                # by-URL eviction would close out from under it.
                time.sleep(0.1)
            return _ok(body, session_id=f"sess-{n}")
        if method == "notifications/initialized":
            return _ok(body)
        if method == "tools/list":
            sid = request.headers.get("mcp-session-id")
            if sid == "sess-1":
                # Rendezvous so both threads' *original* requests are
                # genuinely in flight together -- otherwise one thread can
                # race through discard-and-replace before the other has
                # even sent its request, and never observes `warm` at all.
                expiry_barrier.wait(timeout=2)
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "error": {"code": -32000, "message": "Session expired"},
                    },
                )
            return _ok(body, result={"ok": True})
        raise AssertionError(f"unexpected method: {method}")

    install_transport(handler)

    # Seed a session both threads find already "initialized" with an
    # expired session id, without spending a real initialize call on it.
    warm = mcp_transport._get_session(URL)
    warm.initialized = True
    warm.session_id = "sess-1"
    state["initialize"] = 1  # pretend the warm session came from one prior init

    results: list[dict] = []
    errors: list[BaseException] = []

    def worker():
        try:
            start_barrier.wait()
            results.append(mcp_transport.call(URL, "tools/list"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, errors
    assert len(results) == 2
    assert all(r["result"] == {"ok": True} for r in results)
    # Exactly one shared re-init for the replacement -- not one per thread.
    assert state["initialize"] == 2
    # The replacement both threads converged on must still be open: neither
    # thread closed the other's freshly-initialized session.
    final_session = mcp_transport._sessions[URL]
    assert not final_session.client.is_closed
    assert final_session.session_id == "sess-2"


def test_headers_fn_called_per_request_not_cached(install_transport):
    seen_auth: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _rpc_body(request)
        seen_auth.append(request.headers.get("authorization"))
        if body.get("method") == "initialize":
            return _ok(body, session_id="sess-1")
        if body.get("method") == "notifications/initialized":
            return _ok(body)
        return _ok(body, result={"ok": True})

    install_transport(handler)

    tokens = iter(["token-1", "token-2", "token-3", "token-4"])

    def headers_fn() -> dict[str, str]:
        return {"Authorization": f"Bearer {next(tokens)}"}

    mcp_transport.call(URL, "tools/list", headers_fn=headers_fn)  # initialize + notify + call
    mcp_transport.call(URL, "tools/list", headers_fn=headers_fn)  # already initialized

    assert seen_auth == [
        "Bearer token-1",
        "Bearer token-2",
        "Bearer token-3",
        "Bearer token-4",
    ]
    assert len(set(seen_auth)) == 4  # never reused a cached header


def test_sse_and_plain_json_bodies_both_parse(install_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        body = _rpc_body(request)
        method = body.get("method")
        if method == "initialize":
            return _ok(body, session_id="sess-1")
        if method == "notifications/initialized":
            return _ok(body)
        if method == "tools/list":
            payload = {"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": ["a", "b"]}}
            sse_body = f"event: message\ndata: {json.dumps(payload)}\n\n"
            return httpx.Response(
                200,
                content=sse_body.encode(),
                headers={"content-type": "text/event-stream"},
            )
        if method == "tools/call":
            return _ok(body, result={"ok": True})
        raise AssertionError(f"unexpected method: {method}")

    install_transport(handler)

    sse_result = mcp_transport.call(URL, "tools/list")
    assert sse_result["result"] == {"tools": ["a", "b"]}

    json_result = mcp_transport.call_tool(URL, "noop")
    assert json_result["result"] == {"ok": True}


def test_reset_drops_cache_and_closes_clients(install_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        body = _rpc_body(request)
        if body.get("method") == "initialize":
            return _ok(body, session_id="sess-1")
        if body.get("method") == "notifications/initialized":
            return _ok(body)
        return _ok(body, result={"ok": True})

    install_transport(handler)

    mcp_transport.call(URL, "tools/list")
    session = mcp_transport._sessions[URL]
    assert URL in mcp_transport._sessions

    mcp_transport.reset(URL)

    assert URL not in mcp_transport._sessions
    assert session.client.is_closed

    other_url = URL + "-2"
    mcp_transport.call(URL, "tools/list")
    mcp_transport.call(other_url, "tools/list")
    assert len(mcp_transport._sessions) == 2

    mcp_transport.reset()

    assert mcp_transport._sessions == {}
