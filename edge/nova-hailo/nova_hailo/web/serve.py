"""Run nova_hailo.web.app on one listener, or two when TLS is configured.

Callers: scripts/run_web.sh (python3 -m nova_hailo.web.serve).

TLS off: one plain listener on NOVA_HAILO_HOST:NOVA_HAILO_PORT, exactly the
old `uvicorn nova_hailo.web.app:app` behavior.

TLS on: the Qt HMI still dials plain ws://127.0.0.1:8766, so the plain
listener stays but binds loopback only (never exposed to the LAN), and a
second listener serves wss:// on NOVA_HAILO_TLS_PORT for devices. Both serve
the same app object, so there is one pipeline and one session lock. Only the
first listener runs the lifespan -- running it twice would load the Hailo
pipeline twice.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping

import uvicorn

_TOKEN_RE = re.compile(r"(token=)[^&\s\"]+")


class RedactTokenFilter(logging.Filter):
    """Mask ?token=... in uvicorn's access and websocket log lines.

    Browsers authenticate with a query token (a WebSocket cannot set headers),
    and uvicorn logs every request path verbatim -- which would print the
    shared secret on the Pi's console and in every captured demo log.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_RE.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _TOKEN_RE.sub(r"\1***", record.msg)
        return True


def listener_configs(app, env: Mapping[str, str]) -> list[uvicorn.Config]:
    host = env.get("NOVA_HAILO_HOST", "0.0.0.0")
    port = int(env.get("NOVA_HAILO_PORT", "8766"))
    cert = env.get("NOVA_HAILO_TLS_CERT") or ""
    key = env.get("NOVA_HAILO_TLS_KEY") or ""
    if bool(cert) != bool(key):
        raise SystemExit("Set both NOVA_HAILO_TLS_CERT and NOVA_HAILO_TLS_KEY, or neither.")
    if not cert:
        return [uvicorn.Config(app, host=host, port=port, log_level="info")]
    tls_port = int(env.get("NOVA_HAILO_TLS_PORT", "8767"))
    return [
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info", lifespan="on"),
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=tls_port,
            log_level="info",
            lifespan="off",
            ssl_certfile=cert,
            ssl_keyfile=key,
        ),
    ]


async def run_servers(servers: list[uvicorn.Server]) -> None:
    # Each Server captures SIGINT for itself, so Ctrl-C reaches only one of
    # them; when any listener stops, stop the rest instead of hanging.
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for s in servers:
        s.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    from nova_hailo.web.app import app

    configs = listener_configs(app, os.environ)
    # After Config(): uvicorn's dictConfig runs there and would drop earlier filters.
    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(RedactTokenFilter())
    servers = [uvicorn.Server(c) for c in configs]
    asyncio.run(run_servers(servers))


if __name__ == "__main__":
    main()
