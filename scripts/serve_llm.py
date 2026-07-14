#!/usr/bin/env python3
"""Thin CLI wrapping LlamaSupervisor: start a model profile and keep it running.

Usage: uv run python scripts/serve_llm.py <profile_name> [--config nova/launch/models.yaml]

Starts llama-server for the named profile (per Ctrl-C/SIGTERM, stops it cleanly on exit) and
prints its base URL once /health is passing. Later tasks (s2s wiring) call LlamaSupervisor
directly instead of shelling out to this script; this is a standalone operator convenience.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from nova.launch.llama_supervisor import LlamaSupervisor, LlamaSupervisorError

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "nova" / "launch" / "models.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", help="Model profile name from models.yaml")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to models.yaml")
    parser.add_argument("--health-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    supervisor = LlamaSupervisor(args.config)
    try:
        base_url = supervisor.start(args.profile, health_timeout=args.health_timeout)
    except LlamaSupervisorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"llama-server ready at {base_url} (profile={args.profile})")

    stop_requested = False

    def _request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    try:
        while not stop_requested:
            time.sleep(0.5)
    finally:
        print("stopping llama-server...")
        supervisor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
