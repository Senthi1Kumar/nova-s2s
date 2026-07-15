"""Supervises a detached ``llama-server`` (llama.cpp's OpenAI-compatible server binary) process.

Detached sidecar spawn:
``subprocess.Popen(start_new_session=True)``, the binary resolved explicitly (never assumed
present on ``$PATH`` just because a shell looks "activated" -- shells lie about that), and a
port-in-use check before spawning. Unlike the Python-sidecar variant of that pattern (which
resolves siblings from ``sys.executable``'s directory), ``llama-server`` is an external,
non-Python binary that isn't part of this venv at all, so it is resolved from the
``LLAMA_SERVER_BIN`` env var first, then ``$PATH`` -- see ``_resolve_llama_server_bin``.

This module knows nothing about s2s or the litert-lm edge track; it only starts/stops/swaps one
llama-server process at a time and exposes its base URL.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

load_dotenv()


class LlamaSupervisorError(RuntimeError):
    """Raised for supervisor state-machine violations, config errors, and startup failures."""


@dataclass(frozen=True)
class ModelProfile:
    name: str
    gguf_path: str
    ctx: int
    n_gpu_layers: int
    port: int
    args: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "ModelProfile":
        try:
            return cls(
                name=data["name"],
                gguf_path=data["gguf_path"],
                ctx=data["ctx"],
                n_gpu_layers=data["n_gpu_layers"],
                port=data["port"],
                args=list(data.get("args", [])),
            )
        except KeyError as exc:
            raise LlamaSupervisorError(f"Model profile missing required field: {exc}") from exc


def _resolve_llama_server_bin() -> str:
    """Resolve the llama-server binary path.

    Precedence: 1) ``LLAMA_SERVER_BIN`` env var (must point at an existing file -- checked, not
    just trusted), 2) ``llama-server`` found on ``$PATH`` via ``shutil.which``. The binary is
    never vendored here.
    """
    env_bin = os.environ.get("LLAMA_SERVER_BIN")
    if env_bin:
        if not Path(env_bin).is_file():
            raise LlamaSupervisorError(
                f"LLAMA_SERVER_BIN={env_bin!r} does not exist or is not a file."
            )
        return env_bin
    path_bin = shutil.which("llama-server")
    if path_bin:
        return path_bin
    raise LlamaSupervisorError(
        "llama-server binary not found: set LLAMA_SERVER_BIN or put llama-server on PATH."
    )


def _port_in_use(host: str, port: int) -> bool:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


class LlamaSupervisor:
    """Starts/stops/swaps a single detached llama-server process for one model profile at a time."""

    def __init__(self, config_path: str | Path, *, host: str = "127.0.0.1"):
        self._config_path = Path(config_path)
        self._host = host
        self._profiles = self._load_profiles(self._config_path)
        self._process: subprocess.Popen | None = None
        self._active_profile: ModelProfile | None = None

    @staticmethod
    def _load_profiles(config_path: Path) -> dict[str, ModelProfile]:
        data = yaml.safe_load(config_path.read_text())
        raw_profiles = (data or {}).get("models", [])
        profiles = [ModelProfile.from_dict(p) for p in raw_profiles]
        if not profiles:
            raise LlamaSupervisorError(f"No model profiles found in {config_path}")
        return {profile.name: profile for profile in profiles}

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def base_url(self) -> str:
        if self._active_profile is None:
            raise LlamaSupervisorError("No model started yet; call start() first.")
        return f"http://{self._host}:{self._active_profile.port}"

    def _profile(self, profile_name: str) -> ModelProfile:
        try:
            return self._profiles[profile_name]
        except KeyError:
            raise LlamaSupervisorError(
                f"Unknown model profile {profile_name!r}; known: {sorted(self._profiles)}"
            ) from None

    def start(
        self,
        profile_name: str,
        *,
        health_timeout: float = 30.0,
        poll_interval: float = 0.25,
    ) -> str:
        """Spawn llama-server for ``profile_name`` and block until it passes /health.

        Raises ``LlamaSupervisorError`` if already running (double-start), the profile is
        unknown, the profile's port is already occupied, or health doesn't come up within
        ``health_timeout`` seconds (in which case the spawned process is killed before raising).
        """
        if self.is_running:
            raise LlamaSupervisorError(
                "llama-server already running; call swap() or stop() first."
            )
        profile = self._profile(profile_name)
        if _port_in_use(self._host, profile.port):
            raise LlamaSupervisorError(
                f"Port {profile.port} is already in use; stop whatever is bound to it first."
            )
        binary = _resolve_llama_server_bin()
        argv = [
            binary,
            "-m", profile.gguf_path,
            "-c", str(profile.ctx),
            "-ngl", str(profile.n_gpu_layers),
            "--host", self._host,
            "--port", str(profile.port),
            *profile.args,
        ]
        self._process = subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._active_profile = profile
        try:
            self._wait_for_health(profile, timeout=health_timeout, poll_interval=poll_interval)
        except Exception:
            self._kill_process()
            self._active_profile = None
            raise
        return self.base_url

    def _wait_for_health(
        self, profile: ModelProfile, *, timeout: float, poll_interval: float
    ) -> None:
        deadline = time.monotonic() + timeout
        url = f"http://{self._host}:{profile.port}/health"
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise LlamaSupervisorError(
                    f"llama-server exited early (code {self._process.returncode}) "
                    "before becoming healthy."
                )
            try:
                response = httpx.get(url, timeout=poll_interval)
                if response.status_code == 200:
                    return
            except httpx.HTTPError as exc:
                last_error = exc
            time.sleep(poll_interval)
        raise LlamaSupervisorError(
            f"llama-server did not become healthy within {timeout}s ({url}): {last_error}"
        )

    def _kill_process(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            pgid: int | None = os.getpgid(process.pid)
        except ProcessLookupError:
            pgid = None
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)
            process.wait(timeout=5)
        self._process = None

    def stop(self) -> None:
        """Stop the running llama-server, if any. Idempotent -- a no-op when already stopped."""
        if not self.is_running:
            self._process = None
            self._active_profile = None
            return
        self._kill_process()
        self._active_profile = None

    def swap(self, profile_name: str, **kwargs) -> str:
        """Stop whatever is running (no-op if already stopped) and start ``profile_name``."""
        self.stop()
        return self.start(profile_name, **kwargs)
