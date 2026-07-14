#!/usr/bin/env python3
"""Fake stand-in for the real `llama-server` binary, used only in tests.

Accepts (and ignores) llama-server's real CLI flags (-m, -c, -ngl, --host, --port, plus any extra
``args`` from a model profile) so LlamaSupervisor can spawn it exactly as it would spawn the real
binary. Serves a minimal HTTP API on the requested port:

  GET  /health                -> 200 {"status": "ok"}
  POST /v1/chat/completions   -> 200 fake OpenAI-style chat completion response

Supports extra test hooks: ``--startup-delay SECONDS`` sleeps before serving /health, to
exercise the health-poll timeout path. ``--fail-health`` makes /health always return 503.
``--crash-immediately`` exits the process with a nonzero code before ever binding/serving
/health, to exercise the "process exits early during health-poll" path.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    fail_health = False

    def log_message(self, format, *args):  # noqa: A002 - silence per-request logging
        pass

    def do_GET(self):
        if self.path == "/health":
            if Handler.fail_health:
                self.send_response(503)
                self.end_headers()
                return
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = json.dumps(
                {
                    "id": "fake-completion",
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "fake response"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--model", dest="model", default=None)
    parser.add_argument("-c", "--ctx-size", dest="ctx", default=None)
    parser.add_argument("-ngl", "--n-gpu-layers", dest="n_gpu_layers", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--startup-delay", type=float, default=0.0)
    parser.add_argument("--fail-health", action="store_true")
    parser.add_argument("--crash-immediately", action="store_true")
    # llama-server accepts other flags (e.g. --jinja) we don't care about here.
    args, _unknown = parser.parse_known_args()

    if args.crash_immediately:
        sys.exit(17)

    if args.startup_delay:
        time.sleep(args.startup_delay)

    Handler.fail_health = args.fail_health
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
