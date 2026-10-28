"""`python -m aibox`: HTTPS (0.0.0.0) + loopback HTTP listeners from one process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

import uvicorn

from .app import create_app
from .config import Settings, load_settings
from .services import Services

log = logging.getLogger("aibox")
ROOT = Path(__file__).resolve().parent.parent


def cert_paths(env=None) -> tuple[Path, Path]:
    e = os.environ if env is None else env
    d = Path(e.get("AIBOX_CERT_DIR") or ROOT / "runtime" / "certs")
    return d / "cert.pem", d / "key.pem"


def static_dir(env=None) -> Path:
    e = os.environ if env is None else env
    return Path(e.get("AIBOX_STATIC_DIR") or ROOT / "studio_web")


def listener_configs(settings: Settings, app=None, env=None) -> list[uvicorn.Config]:
    """HTTPS on 0.0.0.0:https_port (self-signed cert) then HTTP on 127.0.0.1:http_port."""
    cert, key = cert_paths(env)
    common = {
        "log_level": "info",
        "access_log": False,  # the inspector records requests; uvicorn's log would leak ?key=
        "proxy_headers": False,  # never let X-Forwarded-For spoof the loopback exemption
        "timeout_graceful_shutdown": 3,
        "ws_max_size": 8 * 1024 * 1024,  # per-message cap (frames/PCM chunks are far smaller)
        "ws_ping_interval": 20,
        "ws_ping_timeout": 20,
    }
    return [
        uvicorn.Config(app, host="0.0.0.0", port=settings.https_port,  # noqa: S104
                       ssl_certfile=str(cert), ssl_keyfile=str(key), **common),
        # lifespan off: the HTTPS server owns the app lifespan (telemetry task) once
        uvicorn.Config(app, host="127.0.0.1", port=settings.http_port, lifespan="off", **common),
    ]


def build_services_or_fake(settings: Settings) -> Services:
    if settings.fake:
        log.warning("AIBOX_FAKE=1: serving FAKE services (no NPU, canned outputs)")
        from .fakes import make_fake_services

        return make_fake_services(settings)
    try:
        from .backends import build_services
    except ModuleNotFoundError as e:
        if e.name not in ("aibox.backends",):
            raise
        log.error("!!! aibox.backends is not importable; falling back to FAKE services !!!")
        from .fakes import make_fake_services

        return make_fake_services(settings)
    return build_services(settings)


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):  # one handler for both servers is installed in serve_all
        yield


async def serve_all(configs: list[uvicorn.Config]) -> None:
    servers = [_Server(c) for c in configs]

    def stop() -> None:
        for s in servers:
            s.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop)
    results = await asyncio.gather(*(s.serve() for s in servers), return_exceptions=True)
    stop()
    for r in results:
        if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
            raise r


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    services = build_services_or_fake(settings)
    sdir = static_dir()
    if not sdir.is_dir():
        log.warning("Studio dir %s not found; / will 404 (API still served)", sdir)
    app = create_app(services, static_dir=sdir)
    cert, key = cert_paths()
    configs = listener_configs(settings, app)
    if not (cert.is_file() and key.is_file()):
        log.error("TLS cert missing (%s); run scripts/gen_cert.sh. Serving loopback HTTP only.",
                  cert)
        configs = configs[1:]
        configs[0].lifespan = "auto"
    log.info("listening: %s", ", ".join(
        f"{'https' if c.ssl_certfile else 'http'}://{c.host}:{c.port}" for c in configs))
    asyncio.run(serve_all(configs))


if __name__ == "__main__":
    main()
