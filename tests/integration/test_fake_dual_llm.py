"""Fake dual-LLM integration: 230M router + 350M articulator (no GPU).

Spins ThreadingHTTPServer stand-ins for llama.cpp OpenAI-compatible endpoints.
Verifies model-mode agent select/execute, articulator receives tools=[], payment
turns cross DriveAuth once with one session identity, and no PIN/trust/sensor
leakage to either LFM.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from nova.server.driveauth_bridge import reset_auth_for_tests
from nova.server.tool_service import app, build_registry, get_registry
from nova.tools.vehicle import VehicleDB

# Do not flag tool-schema prose ("biometric authorization"); flag raw sensors.
_LEAK_RE = re.compile(
    r"embedding|ndarray|\.tolist\(|trust_vector|raw_sensor|fingerprint_template",
    re.I,
)


class _FakeLLMServer:
    """Minimal OpenAI-compatible /v1 chat + /models server."""

    def __init__(self, role: str, responder):
        self.role = role
        self.responder = responder
        self.requests: list[dict[str, Any]] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def start(self) -> str:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                return

            def do_GET(self):
                if self.path in {"/models", "/v1/models", "/health"}:
                    body = json.dumps({"data": [{"id": parent.role}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode() or "{}")
                except json.JSONDecodeError:
                    payload = {}
                parent.requests.append(payload)
                path = self.path
                if path.endswith("/chat/completions") or path == "/v1/chat/completions":
                    out = parent.responder(payload)
                    body = json.dumps(out).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()


def _router_response(payload: dict[str, Any]) -> dict[str, Any]:
    """230M: pick a tool from the user message (deterministic)."""
    msgs = payload.get("messages") or []
    user = ""
    for m in msgs:
        if m.get("role") == "user":
            user = str(m.get("content") or "")
    tools = payload.get("tools") or []
    assert tools, "router must receive the toolbox"
    low = user.lower()
    name, args = "check_email", {"mode": "unread"}
    if "calendar" in low or "tomorrow" in low:
        name, args = "check_calendar", {"day": "tomorrow"}
    elif "stock" in low or "amazon" in low:
        name, args = "web_search", {"query": user}
    elif "pay" in low or "rupees" in low:
        name, args = "send_payment", {"payee": "Chai Point", "amount": 50}
    elif "drive" in low:
        name, args = "list_drive_files", {}
    return {
        "id": "fake-router",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


def _articulator_response(payload: dict[str, Any]) -> dict[str, Any]:
    tools = payload.get("tools")
    # Speak path must not re-offer the toolbox (live LFM markup failure).
    assert tools in ([], None) or payload.get("tool_choice") == "none"
    if tools:
        assert tools == []
    text = "Understood."
    for m in payload.get("messages") or []:
        content = str(m.get("content") or "")
        if "Tool results" in content or "already fetched" in content:
            text = content.split("\n")[-1][:200] or text
            break
    return {
        "id": "fake-articulator",
        "choices": [
            {
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }


@pytest.fixture()
def dual_llm(tmp_path, monkeypatch):
    router = _FakeLLMServer("tool-router-230m", _router_response)
    articulator = _FakeLLMServer("articulator-350m", _articulator_response)
    router_url = router.start()
    articulator_url = articulator.start()

    monkeypatch.setenv("DRIVEAUTH_USE_MOCK", "1")
    monkeypatch.setenv("DRIVEAUTH_SEED_MATURE", "1")
    monkeypatch.setenv("DRIVEAUTH_STORE_DIR", str(tmp_path / "da"))
    monkeypatch.setenv("DRIVEAUTH_DRIVER_ID", "driver1")
    monkeypatch.setenv("NOVA_TOOL_ROUTE_MODE", "model")
    monkeypatch.setenv("NOVA_ROUTER_LLM_URL", router_url)
    reset_auth_for_tests()

    reg = build_registry(
        VehicleDB(tmp_path / "v.db"),
        driveauth_store=tmp_path / "da",
    )
    app.dependency_overrides[get_registry] = lambda: reg
    from nova.server import tool_service as ts

    ts._pending_confirm.clear()
    client = TestClient(app)
    yield {
        "client": client,
        "reg": reg,
        "router": router,
        "articulator": articulator,
        "articulator_url": articulator_url,
        "router_url": router_url,
    }
    app.dependency_overrides.clear()
    ts._pending_confirm.clear()
    reset_auth_for_tests()
    router.stop()
    articulator.stop()


def _assert_no_secrets(payload: dict[str, Any], *, scan_tools: bool = False) -> None:
    """Scan messages / top-level fields; optionally skip bulky tool schemas."""
    if scan_tools:
        blob = json.dumps(payload, default=str)
    else:
        slim = {k: v for k, v in payload.items() if k != "tools"}
        blob = json.dumps(slim, default=str)
    assert not _LEAK_RE.search(blob), blob[:300]
    assert "4242" not in blob


@pytest.mark.component
def test_fake_dual_llm_select_execute_articulate(dual_llm):
    client = dual_llm["client"]
    reg = dual_llm["reg"]
    articulator = dual_llm["articulator"]
    articulator_url = dual_llm["articulator_url"]

    fake_result = {"speak": "Amazon is near one eighty.", "ok": True}

    # Avoid lexical calendar/email/web short-circuits — this asserts the 230M path.
    agent = client.post(
        "/tools/agent",
        json={
            "query": "List my Google Drive files.",
            "execute": True,
            "session_id": "dual-1",
        },
    ).json()

    assert agent["needs_tools"] is True
    assert agent["tool_calls"][0]["name"] == "list_drive_files"
    assert agent["tool_choice"] == "none"
    assert dual_llm["router"].requests, "230M router should have been called"
    router_req = dual_llm["router"].requests[-1]
    assert router_req.get("tools"), "230M must see tools"
    _assert_no_secrets(router_req)

    speak_body = {
        "model": "articulator-350m",
        "messages": [
            {"role": "user", "content": "List my Google Drive files."},
            {
                "role": "user",
                "content": (
                    "[Tool results already fetched — speak these to the driver now. "
                    "Do not call tools. Do not invent numbers.]\n"
                    + agent["speak_payload"]
                ),
            },
        ],
        "tools": [],
        "tool_choice": "none",
    }
    with httpx.Client(timeout=5.0) as http:
        resp = http.post(f"{articulator_url}/chat/completions", json=speak_body)
        resp.raise_for_status()
        art_out = resp.json()
    assert articulator.requests
    last = articulator.requests[-1]
    assert last.get("tools") == []
    assert last.get("tool_choice") == "none"
    _assert_no_secrets(last)
    content = art_out["choices"][0]["message"]["content"]
    assert content

    with patch.object(reg["web_search"], "execute", return_value=fake_result) as sex:
        stock = client.post(
            "/tools/agent",
            json={"query": "Tell me the current stock price of Amazon.", "execute": True},
        ).json()
    assert stock["tool_calls"][0]["name"] == "web_search"
    sex.assert_called_once()


@pytest.mark.component
def test_fake_dual_llm_payment_gates_once_same_session(dual_llm):
    client = dual_llm["client"]
    articulator_url = dual_llm["articulator_url"]
    session_id = "pay-sess-dual"

    # Force ACCEPT so this contract stays independent of DriveAuth OOD flakiness.
    accept_payload = {
        "status": "accept",
        "decision": "ACCEPT",
        "is_payment": True,
        "session_id": session_id,
        "speak": "",
        "trust": 0.91,
        "risk": 0.05,
        "tier": "payment",
        "rule": "mock_accept",
    }
    with (
        patch("nova.server.driveauth_bridge.precheck", return_value=accept_payload),
        patch(
            "nova.tools.payment.require_payment_auth",
            return_value=accept_payload,
        ),
    ):
        pre = client.post(
            "/tools/auth/precheck",
            json={
                "session_id": session_id,
                "transcript": "Pay 50 rupees to Chai Point.",
            },
        ).json()
        assert pre["status"] == "accept"
        _assert_no_secrets(pre)

        out = client.post(
            "/tools/execute",
            json={
                "name": "send_payment",
                "args": {
                    "payee": "Chai Point",
                    "amount": 50,
                    "beneficiary_known": True,
                    "session_id": session_id,
                },
            },
        ).json()

        assert out["status"] == "sent"
        assert out.get("auth", {}).get("decision") == "accept"

        again = client.post(
            "/tools/execute",
            json={
                "name": "send_payment",
                "args": {
                    "payee": "Chai Point",
                    "amount": 40,
                    "beneficiary_known": True,
                    "session_id": session_id,
                },
            },
        ).json()
        assert again["status"] == "sent"
        assert again.get("auth", {}).get("decision") == "accept"

    for req in dual_llm["router"].requests:
        _assert_no_secrets(req)
    speak_body = {
        "model": "articulator-350m",
        "messages": [
            {"role": "user", "content": "Pay 50 rupees to Chai Point."},
            {
                "role": "user",
                "content": "[Tool results]\n" + out["speak"],
            },
        ],
        "tools": [],
        "tool_choice": "none",
    }
    with httpx.Client(timeout=5.0) as http:
        http.post(f"{articulator_url}/chat/completions", json=speak_body).raise_for_status()
    for req in dual_llm["articulator"].requests:
        _assert_no_secrets(req)
        assert req.get("tools") == []
    assert "4242" not in json.dumps(dual_llm["router"].requests)
    assert "4242" not in json.dumps(dual_llm["articulator"].requests)


@pytest.mark.component
def test_fake_dual_llm_route_strips_toolbox_in_model_mode(dual_llm):
    client = dual_llm["client"]
    route = client.post(
        "/tools/route",
        json={"query": "Check my emails.", "k": 8},
    ).json()
    assert route["tool_choice"] == "none"
    assert isinstance(route["tools"], list)
