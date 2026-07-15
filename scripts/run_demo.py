#!/usr/bin/env python3
"""Single entrypoint for the Nova demo (s2s pivot): brings up llama-server,
the s2s realtime WebSocket server, and Nova's tool/UI service, wired together
per ``nova/config.yaml`` (override with ``NOVA_CONFIG=/path/to.yaml``).

    uv run python scripts/run_demo.py
    NOVA_CONFIG=nova/config.thor.yaml uv run python scripts/run_demo.py

Fails clearly (not a silent hang) if ``LLAMA_SERVER_BIN``/the configured GGUF
is missing, or if any of the three processes doesn't come up healthy within
its timeout. Ctrl-C stops all three cleanly.

Never imports ``nova.engine`` or ``nova.server.app``/``nova.server.workers``
Uses ``nova.launch.llama_supervisor``
and a subprocess-launched ``nova.server.tool_service`` / s2s realtime server.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType

import httpx
import websockets
import yaml
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from nova.launch.llama_supervisor import LlamaSupervisor, LlamaSupervisorError  # noqa: E402
from nova.launch.prewarm import prewarm_llm  # noqa: E402


def _config_path() -> Path:
    raw = (os.environ.get("NOVA_CONFIG") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (REPO_ROOT / p)
    return REPO_ROOT / "nova" / "config.yaml"


CONFIG_PATH = _config_path()
MODELS_YAML = REPO_ROOT / "nova" / "launch" / "models.yaml"
SOUL_PATH = REPO_ROOT / "prompts" / "soul.md"

HTTP_READY_TIMEOUT_S = 30.0
WS_READY_TIMEOUT_S = 300.0  # first FunASR/SenseVoice weight download can exceed 180s


def _fail(message: str) -> "None":
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def resolve_stt_device(cfg: dict) -> str:
    """STT device from config; 'cpu' when unset. Actual CUDA-OOM fallback is
    handled at s2s-process-exit time in _start_s2s (a cuda load that dies is
    retried once on cpu)."""
    dev = str(cfg.get("stt_device", "cpu")).lower()
    return "cuda" if dev == "cuda" else "cpu"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        _fail(f"Config not found at {CONFIG_PATH}")
    print(f"Loading config from {CONFIG_PATH}")
    data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    for required in ("llm_profile",):
        if required not in data:
            _fail(f"{CONFIG_PATH} missing required key: {required!r}")
    return data



def _driveauth_env(cfg: dict) -> dict[str, str]:
    """Env for tool-service / s2s DriveAuth mock (fail-closed if misconfigured)."""
    da = cfg.get("driveauth") or {}
    use_mock = da.get("use_mock", True)
    if isinstance(use_mock, str):
        use_mock = use_mock.strip().lower() not in {"0", "false", "off", "no"}
    store = str(da.get("store_dir") or "runtime/driveauth_store")
    out = {
        "DRIVEAUTH_USE_MOCK": "1" if use_mock else "0",
        "DRIVEAUTH_SEED_MATURE": "1" if da.get("seed_mature", True) else "0",
        "DRIVEAUTH_STORE_DIR": store,
        "DRIVEAUTH_DRIVER_ID": str(da.get("driver_id") or "driver1"),
    }
    enroll = da.get("enroll_dir")
    if enroll:
        out["DRIVEAUTH_ENROLL_DIR"] = str(enroll)
    if not use_mock and not enroll:
        print("  WARN: driveauth.use_mock=false without enroll_dir — payments fail closed")
    return out


class DemoStack:
    """Owns the three demo processes and their clean startup/shutdown."""

    def __init__(self, config: dict):
        self.config = config
        self.supervisor = LlamaSupervisor(MODELS_YAML)
        self.router_supervisor = LlamaSupervisor(MODELS_YAML)
        self.tool_service_proc: subprocess.Popen | None = None
        self.s2s_proc: subprocess.Popen | None = None
        self._stt_fallback_attempted: bool = False
        self._router_base_url: str | None = None

    # -- startup -----------------------------------------------------------

    def start(self) -> None:
        cfg = self.config
        base_url = self._start_llama_server(cfg["llm_profile"])
        self._start_router_llm(cfg)
        tool_host, tool_port = self._start_tool_service(cfg)
        ws_host, ws_port = self._start_s2s(cfg, base_url, tool_host, tool_port)
        self._prewarm_llm(base_url, tool_host, tool_port)

        print()
        print("Nova demo is up.")
        print(f"  Open:            http://{tool_host}:{tool_port}/")
        print(f"  Realtime WS:     ws://{ws_host}:{ws_port}/v1/realtime")
        print(f"  llama-server:    {base_url}")
        if self._router_base_url:
            print(f"  tool-router:     {self._router_base_url}")
        print("Press Ctrl-C to stop everything.")

    def _start_llama_server(self, profile_name: str) -> str:
        print(f"Starting llama-server (profile={profile_name!r}) ...")
        try:
            base_url = self.supervisor.start(profile_name)
        except LlamaSupervisorError as exc:
            _fail(str(exc))
        print(f"  OK: llama-server healthy at {base_url}")
        return base_url

    def _start_router_llm(self, cfg: dict) -> str | None:
        """Optional second llama-server for tool_route_mode=model (230M CPU)."""
        route_mode = str(
            cfg.get("tool_route_mode") or os.environ.get("NOVA_TOOL_ROUTE_MODE") or "full"
        )
        if route_mode not in {"model", "agent", "router"}:
            self._router_base_url = None
            return None
        profile = str(cfg.get("router_llm_profile") or "lfm2.5-230m-q4")
        print(f"Starting tool-router llama-server (profile={profile!r}) ...")
        try:
            base_url = self.router_supervisor.start(profile, health_timeout=60.0)
        except LlamaSupervisorError as exc:
            _fail(str(exc))
        self._router_base_url = base_url
        print(f"  OK: tool-router healthy at {base_url}")
        return base_url

    def _start_tool_service(self, cfg: dict) -> tuple[str, int]:
        host = cfg.get("tool_service_host", "127.0.0.1")
        port = cfg.get("tool_service_port", 8000)
        # full = all tools + tool_choice=auto (no lexical force). Override with
        # NOVA_TOOL_ROUTE_MODE=force to restore named force + short-circuit.
        route_mode = str(cfg.get("tool_route_mode") or os.environ.get("NOVA_TOOL_ROUTE_MODE") or "full")
        print(f"Starting Nova tool/UI service on {host}:{port} (route_mode={route_mode}) ...")
        env = {**os.environ, "NOVA_TOOL_ROUTE_MODE": route_mode, **_driveauth_env(cfg)}
        if self._router_base_url:
            env["NOVA_ROUTER_LLM_URL"] = f"{self._router_base_url}/v1"
        self.tool_service_proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "nova.server.tool_service:app",
                "--host", host, "--port", str(port),
            ],
            cwd=REPO_ROOT,
            start_new_session=True,
            env=env,
        )
        self._wait_http(
            self.tool_service_proc,
            f"http://{host}:{port}/tools/schemas",
            "Nova tool/UI service",
            HTTP_READY_TIMEOUT_S,
        )
        print("  OK: Nova tool/UI service ready")
        return host, port

    def _start_s2s(
        self, cfg: dict, llama_base_url: str, tool_host: str, tool_port: int
    ) -> tuple[str, int]:
        ws_host = cfg.get("ws_host", "127.0.0.1")
        ws_port = cfg.get("ws_port", 8765)
        stt = cfg.get("stt", "parakeet-tdt")
        tts = cfg.get("tts", "kokoro")
        argv = [
            sys.executable, "-m", "speech_to_speech.s2s_pipeline",
            "--mode", "realtime",
            "--llm_backend", "chat-completions",
            "--responses_api_base_url", f"{llama_base_url}/v1",
            # llama-server needs no API key, but the OpenAI SDK client s2s's
            # ChatCompletionsApiModelHandler builds around raises OpenAIError
            # if api_key is left unset (confirmed by a real run) -- any
            # non-empty placeholder string satisfies it.
            "--responses_api_api_key", "not-needed",
            "--model_name", cfg["llm_profile"],
            "--stt", stt,
            "--tts", tts,
            "--ws_host", ws_host,
            "--ws_port", str(ws_port),
            # s2s's history trim/compaction fires on USER-TURN COUNT
            # (chat_size), not token count. The default (30) let a real
            # session with a few verbose turns (e.g. a 10-point news list)
            # blow past ctx=4096 in raw tokens well before 30 turns
            # accumulated, crashing the turn with a hard context-size error
            # and zero audio. Trimming every few turns keeps raw prompt size
            # bounded regardless of per-turn verbosity.
            "--chat_size", str(cfg.get("chat_size", 8)),
        ]
        stt_device = resolve_stt_device(cfg)
        if stt == "parakeet-tdt":
            argv += ["--parakeet_tdt_device", stt_device]
        elif stt == "sensevoice":
            argv += [
                "--sensevoice_stt_model_name",
                cfg.get("sensevoice_model", "FunAudioLLM/SenseVoiceSmall"),
                "--sensevoice_stt_device",
                stt_device,
                "--sensevoice_stt_language",
                str(cfg.get("sensevoice_language", "en")),
            ]
        elif stt == "paraformer":
            # Legacy FunASR Paraformer / Fun-ASR-Nano path.
            argv += [
                "--paraformer_stt_model_name",
                cfg.get("paraformer_model", "paraformer-en"),
                "--paraformer_stt_device",
                stt_device,
            ]
            lang = cfg.get("paraformer_language")
            if lang:
                argv += ["--paraformer_stt_language", str(lang)]
        elif stt == "faster-whisper":
            # Optional path: requires speech-to-speech[faster-whisper] / faster-whisper
            # installed. Parakeet-TDT-0.6B on GPU is a known OOM risk on this 4 GiB
            # card; faster-whisper "small" is well under 1 GiB if that extra is present.
            argv += [
                "--faster_whisper_stt_model_name", cfg.get("faster_whisper_model", "small"),
                "--faster_whisper_stt_device", stt_device,
                "--faster_whisper_stt_compute_type", "int8_float16" if stt_device == "cuda" else "int8",
            ]
        if tts == "kokoro" and cfg.get("tts_device"):
            argv += ["--kokoro_device", cfg["tts_device"]]
        if tts == "pocket" and cfg.get("tts_device"):
            argv += ["--pocket_tts_device", cfg["tts_device"]]

        print("Starting s2s realtime server ...")
        print("  " + " ".join(argv))
        # s2s's LLM client defaults to a 20s read timeout; on this box a slow-but-
        # eventually-successful follow-up generation (after tool-result reinjection)
        # can exceed that and silently produce zero audio (see the 2026-07-09 demo
        # runbook's run5). Raise it for the s2s subprocess specifically.
        # LLM handler POSTs here before each generation so tool_choice is not
        # raced by the client's post-transcript session.update (see s2s
        # base_openai_compatible_language_model._nova_route_override).
        route_mode = str(cfg.get("tool_route_mode") or os.environ.get("NOVA_TOOL_ROUTE_MODE") or "full")
        env = {
            **os.environ,
            **_driveauth_env(cfg),
            "S2S_LLM_REQUEST_TIMEOUT_S": "90",
            "NOVA_TOOLS_ROUTE_URL": f"http://{tool_host}:{tool_port}/tools/route",
            "NOVA_TOOLS_AGENT_URL": f"http://{tool_host}:{tool_port}/tools/agent",
            "NOVA_TOOLS_AUTH_PRECHECK_URL": f"http://{tool_host}:{tool_port}/tools/auth/precheck",
            "NOVA_TOOL_ROUTE_MODE": route_mode,
            # full/model: never invent args / skip the model
            "NOVA_FORCE_TOOL_SHORTCIRCUIT": (
                "0" if route_mode in {"full", "off", "auto", "all", "model", "agent", "router"} else "1"
            ),
        }
        if self._router_base_url:
            env["NOVA_ROUTER_LLM_URL"] = f"{self._router_base_url}/v1"
        self.s2s_proc = subprocess.Popen(argv, cwd=REPO_ROOT, start_new_session=True, env=env)
        try:
            self._wait_ws(ws_host, ws_port, WS_READY_TIMEOUT_S)
        except SystemExit:
            if stt_device == "cuda" and not self._stt_fallback_attempted:
                self._stt_fallback_attempted = True
                print("CUDA STT load failed, retrying on CPU")
                return self._start_s2s({**cfg, "stt_device": "cpu"}, llama_base_url, tool_host, tool_port)
            raise
        print("  OK: s2s realtime server ready")
        return ws_host, ws_port

    def _prewarm_llm(self, base_url: str, tool_host: str, tool_port: int) -> None:
        try:
            soul_text = SOUL_PATH.read_text()
            schemas = httpx.get(
                f"http://{tool_host}:{tool_port}/tools/schemas", timeout=5.0
            ).json()
            prewarm_llm(base_url, soul_text, schemas)
            print("  OK: llama prefix cache warmed")
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN: llama prefix cache warm skipped ({exc})")

    # -- readiness polling ---------------------------------------------------

    def _wait_http(
        self, proc: subprocess.Popen, url: str, label: str, timeout: float
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.stop()
                _fail(f"{label} exited early (code {proc.returncode}) before becoming ready.")
            try:
                resp = httpx.get(url, timeout=1.0)
                if resp.status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        self.stop()
        _fail(f"{label} did not become ready within {timeout}s at {url}.")

    def _wait_ws(self, host: str, port: int, timeout: float) -> None:
        uri = f"ws://{host}:{port}/v1/realtime"
        deadline = time.monotonic() + timeout
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            if self.s2s_proc is not None and self.s2s_proc.poll() is not None:
                self.stop()
                _fail(
                    f"s2s realtime server exited early (code {self.s2s_proc.returncode}) "
                    "before becoming ready. Run it directly (see the printed command above) "
                    "to see its stdout/stderr."
                )
            try:
                asyncio.run(self._check_ws(uri))
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(1.0)
        self.stop()
        _fail(f"s2s realtime server did not become ready within {timeout}s at {uri} ({last_exc}).")

    @staticmethod
    async def _check_ws(uri: str) -> None:
        async with websockets.connect(uri, open_timeout=2.0):
            pass

    # -- shutdown ------------------------------------------------------------

    def stop(self) -> None:
        for proc, label in (
            (self.s2s_proc, "s2s realtime server"),
            (self.tool_service_proc, "Nova tool/UI service"),
        ):
            if proc is not None and proc.poll() is None:
                print(f"Stopping {label} ...")
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        self.router_supervisor.stop()
        self.supervisor.stop()


def main() -> None:
    config = load_config()
    stack = DemoStack(config)

    def _handle_signal(signum: int, frame: FrameType | None) -> None:
        print("\nShutting down ...")
        stack.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    stack.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stack.stop()


if __name__ == "__main__":
    main()
