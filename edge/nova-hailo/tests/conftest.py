"""Suite-wide guard against accidental real network egress.

Every test in this suite is expected to mock at the httpx/mcp_transport
layer (see tests/test_connectors.py, tests/test_workspace_gmail.py,
tests/test_mcp_transport.py, etc.) rather than actually dialling out. This
branch's own history includes a green-looking suite that spent real seconds
on real third-party network calls after a mock seam quietly broke, with
nothing in the run to flag it. The autouse fixture below turns that failure
mode into an immediate, named test failure instead of a silent slow pass.

Only `socket.socket.connect`/`connect_ex` are patched -- not `socket()`
construction, not DNS resolution (`getaddrinfo`), and not AF_UNIX sockets
(local IPC, if any code ever uses it) or subprocess pipes, which never go
through this path at all. Only an actual outbound (or loopback) TCP/UDP
connect attempt trips this.

A test that genuinely needs real network egress can opt out with
`@pytest.mark.allow_network`. Nothing in this suite carries that marker
today -- if making a test pass requires adding it, that means the test is
reaching the real network today, which is itself worth reporting rather
than silently working around.
"""
from __future__ import annotations

import socket
from typing import Any

import pytest

_ALLOW_NETWORK_MARKER = "allow_network"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{_ALLOW_NETWORK_MARKER}: opt this test out of the autouse "
        "real-network guard in tests/conftest.py (use only when the test "
        "genuinely needs real network egress).",
    )


@pytest.fixture(autouse=True)
def _block_real_network_connections(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
):
    if request.node.get_closest_marker(_ALLOW_NETWORK_MARKER):
        yield
        return

    test_id = request.node.nodeid
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    unix_family = getattr(socket, "AF_UNIX", None)

    def _guarded(name: str, real_fn):
        def _call(self: socket.socket, address: Any, *args: Any, **kwargs: Any):
            # Local unix-domain sockets are not the "real network" this
            # guard exists to catch; let them through untouched.
            if unix_family is not None and self.family == unix_family:
                return real_fn(self, address, *args, **kwargs)
            raise RuntimeError(
                f"{test_id} attempted a real network connection "
                f"(socket.{name} to {address!r}). Tests must mock the "
                "transport (httpx/mcp_transport) instead of dialling out; "
                "mark the test @pytest.mark.allow_network only if it "
                "genuinely needs real egress."
            )

        return _call

    monkeypatch.setattr(socket.socket, "connect", _guarded("connect", real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded("connect_ex", real_connect_ex))
    yield
