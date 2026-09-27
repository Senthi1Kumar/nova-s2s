"""serve.py: loopback plain + LAN TLS over one app, lifespan once."""
from __future__ import annotations

import asyncio
import socket
import ssl
import subprocess
import threading
import time
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Other session tests (test_barge_in) install empty fastapi/starlette stubs at
# import time; this file needs the real packages to run real listeners.
for _mod in ("fastapi", "starlette", "starlette.websockets"):
    if _mod in sys.modules and not getattr(sys.modules[_mod], "__file__", None):
        del sys.modules[_mod]

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from nova_hailo.web.serve import listener_configs, run_servers

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def tls_dir(tmp_path):
    out = subprocess.run(
        ["bash", str(ROOT / "scripts/make_lan_cert.sh")],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "NOVA_HAILO_TLS_DIR": str(tmp_path / "tls"), "NOVA_HAILO_CERT_IP": "127.0.0.1"},
        capture_output=True, text=True, check=True,
    ).stdout
    assert "sha256: " in out
    assert oct((tmp_path / "tls/key.pem").stat().st_mode)[-3:] == "600"
    return tmp_path / "tls"


def test_tls_off_is_single_listener_unchanged():
    cfgs = listener_configs(FastAPI(), {"NOVA_HAILO_HOST": "0.0.0.0", "NOVA_HAILO_PORT": "8766"})
    assert [(c.host, c.port, c.ssl_certfile) for c in cfgs] == [("0.0.0.0", 8766, None)]


def test_tls_on_splits_loopback_plain_and_lan_tls():
    cfgs = listener_configs(FastAPI(), {
        "NOVA_HAILO_HOST": "0.0.0.0", "NOVA_HAILO_PORT": "8766",
        "NOVA_HAILO_TLS_CERT": "/c.pem", "NOVA_HAILO_TLS_KEY": "/k.pem",
    })
    assert [(c.host, c.port, c.ssl_certfile, c.lifespan) for c in cfgs] == [
        ("127.0.0.1", 8766, None, "on"),
        ("0.0.0.0", 8767, "/c.pem", "off"),
    ]


def test_half_configured_tls_refuses_to_start():
    with pytest.raises(SystemExit):
        listener_configs(FastAPI(), {"NOVA_HAILO_TLS_CERT": "/c.pem"})


def _app_with_counter():
    starts = []

    @asynccontextmanager
    async def lifespan(app):
        starts.append(1)
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/ping")
    async def ping():
        return {"id": id(app)}

    return app, starts


def _serve_in_thread(servers):
    t = threading.Thread(target=lambda: asyncio.run(run_servers(servers)), daemon=True)
    t.start()
    deadline = time.time() + 10
    while not all(s.started for s in servers):
        assert time.time() < deadline, "listeners did not start"
        time.sleep(0.05)
    return t


def _tls_env(tls_dir, plain, tls):
    return {
        "NOVA_HAILO_PORT": str(plain), "NOVA_HAILO_TLS_PORT": str(tls),
        "NOVA_HAILO_TLS_CERT": str(tls_dir / "cert.pem"),
        "NOVA_HAILO_TLS_KEY": str(tls_dir / "key.pem"),
    }


# Loopback-only sockets: the listeners under test are local.
@pytest.mark.allow_network
def test_lifespan_runs_once_across_two_listeners(tls_dir):
    app, starts = _app_with_counter()
    plain, tls = _free_port(), _free_port()
    servers = [uvicorn.Server(c) for c in listener_configs(app, _tls_env(tls_dir, plain, tls))]
    t = _serve_in_thread(servers)
    try:
        a = httpx.get(f"http://127.0.0.1:{plain}/ping").json()
        ctx = ssl.create_default_context(cafile=str(tls_dir / "cert.pem"))
        b = httpx.get(f"https://127.0.0.1:{tls}/ping", verify=ctx).json()
        assert a == b  # same app object behind both ports
        assert starts == [1]
    finally:
        for s in servers:
            s.should_exit = True
        t.join(timeout=10)


# Loopback-only sockets: the listeners under test are local.
@pytest.mark.allow_network
def test_stopping_one_listener_stops_all(tls_dir):
    app, _ = _app_with_counter()
    servers = [uvicorn.Server(c) for c in
               listener_configs(app, _tls_env(tls_dir, _free_port(), _free_port()))]
    t = _serve_in_thread(servers)
    servers[1].should_exit = True
    t.join(timeout=10)
    assert not t.is_alive()


def test_access_log_redacts_token():
    import logging

    from nova_hailo.web.serve import RedactTokenFilter

    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.2:5", "GET", "/config?token=s3cret&x=1", "1.1", 200), None,
    )
    assert RedactTokenFilter().filter(rec) is True
    msg = rec.getMessage()
    assert "s3cret" not in msg
    assert "token=***&x=1" in msg
