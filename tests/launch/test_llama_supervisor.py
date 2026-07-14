from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import httpx
import pytest
import yaml

from nova.launch.llama_supervisor import (
    LlamaSupervisor,
    LlamaSupervisorError,
    ModelProfile,
    _port_in_use,
)

FAKE_BIN = str(Path(__file__).resolve().parent / "fixtures" / "fake_llama_server.py")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_config(tmp_path: Path, profiles: list[dict]) -> Path:
    config_path = tmp_path / "models.yaml"
    config_path.write_text(yaml.safe_dump({"models": profiles}))
    return config_path


@pytest.fixture
def fake_bin(monkeypatch):
    monkeypatch.setenv("LLAMA_SERVER_BIN", FAKE_BIN)


# --- config parsing ---------------------------------------------------------


def test_load_profiles_parses_config(tmp_path):
    config_path = _write_config(
        tmp_path,
        [
            {
                "name": "a",
                "gguf_path": "/models/a.gguf",
                "ctx": 4096,
                "n_gpu_layers": 999,
                "port": 18080,
                "args": ["--jinja"],
            },
            {"name": "b", "gguf_path": "/models/b.gguf", "ctx": 2048, "n_gpu_layers": 0, "port": 18081},
        ],
    )
    supervisor = LlamaSupervisor(config_path)
    assert set(supervisor._profiles) == {"a", "b"}
    assert supervisor._profiles["a"] == ModelProfile(
        "a", "/models/a.gguf", 4096, 999, 18080, ["--jinja"]
    )
    assert supervisor._profiles["b"].args == []


def test_load_profiles_empty_raises(tmp_path):
    config_path = _write_config(tmp_path, [])
    with pytest.raises(LlamaSupervisorError):
        LlamaSupervisor(config_path)


def test_load_profiles_missing_field_raises(tmp_path):
    config_path = _write_config(tmp_path, [{"name": "a", "gguf_path": "x", "ctx": 1}])
    with pytest.raises(LlamaSupervisorError):
        LlamaSupervisor(config_path)


def test_real_models_yaml_parses():
    """The repo's shipped example profile should itself parse cleanly."""
    config_path = Path(__file__).resolve().parents[2] / "nova" / "launch" / "models.yaml"
    supervisor = LlamaSupervisor(config_path)
    assert len(supervisor._profiles) >= 1


# --- state machine / lifecycle ----------------------------------------------


def test_start_unknown_profile_raises(tmp_path, fake_bin):
    config_path = _write_config(
        tmp_path, [{"name": "a", "gguf_path": "x", "ctx": 1, "n_gpu_layers": 0, "port": _free_port()}]
    )
    supervisor = LlamaSupervisor(config_path)
    with pytest.raises(LlamaSupervisorError):
        supervisor.start("nope")


def test_base_url_before_start_raises(tmp_path, fake_bin):
    config_path = _write_config(
        tmp_path, [{"name": "a", "gguf_path": "x", "ctx": 1, "n_gpu_layers": 0, "port": _free_port()}]
    )
    supervisor = LlamaSupervisor(config_path)
    with pytest.raises(LlamaSupervisorError):
        _ = supervisor.base_url


def test_start_polls_health_and_exposes_base_url(tmp_path, fake_bin):
    port = _free_port()
    config_path = _write_config(
        tmp_path, [{"name": "a", "gguf_path": "x", "ctx": 1, "n_gpu_layers": 0, "port": port}]
    )
    supervisor = LlamaSupervisor(config_path)
    try:
        base_url = supervisor.start("a", health_timeout=10.0, poll_interval=0.1)
        assert base_url == f"http://127.0.0.1:{port}"
        assert supervisor.base_url == base_url
        assert supervisor.is_running
        health = httpx.get(f"{base_url}/health", timeout=2.0)
        assert health.status_code == 200
    finally:
        supervisor.stop()
    assert not supervisor.is_running


def test_double_start_raises(tmp_path, fake_bin):
    port = _free_port()
    config_path = _write_config(
        tmp_path, [{"name": "a", "gguf_path": "x", "ctx": 1, "n_gpu_layers": 0, "port": port}]
    )
    supervisor = LlamaSupervisor(config_path)
    try:
        supervisor.start("a", health_timeout=10.0, poll_interval=0.1)
        with pytest.raises(LlamaSupervisorError):
            supervisor.start("a", health_timeout=10.0, poll_interval=0.1)
    finally:
        supervisor.stop()


def test_stop_when_never_started_is_noop(tmp_path):
    config_path = _write_config(
        tmp_path, [{"name": "a", "gguf_path": "x", "ctx": 1, "n_gpu_layers": 0, "port": _free_port()}]
    )
    supervisor = LlamaSupervisor(config_path)
    supervisor.stop()
    assert not supervisor.is_running


def test_stop_twice_is_idempotent(tmp_path, fake_bin):
    port = _free_port()
    config_path = _write_config(
        tmp_path, [{"name": "a", "gguf_path": "x", "ctx": 1, "n_gpu_layers": 0, "port": port}]
    )
    supervisor = LlamaSupervisor(config_path)
    supervisor.start("a", health_timeout=10.0, poll_interval=0.1)
    supervisor.stop()
    supervisor.stop()
    assert not supervisor.is_running


def test_swap_stops_old_and_starts_new(tmp_path, fake_bin):
    port_a = _free_port()
    port_b = _free_port()
    config_path = _write_config(
        tmp_path,
        [
            {"name": "a", "gguf_path": "x", "ctx": 1, "n_gpu_layers": 0, "port": port_a},
            {"name": "b", "gguf_path": "y", "ctx": 1, "n_gpu_layers": 0, "port": port_b},
        ],
    )
    supervisor = LlamaSupervisor(config_path)
    try:
        supervisor.start("a", health_timeout=10.0, poll_interval=0.1)
        assert supervisor.base_url == f"http://127.0.0.1:{port_a}"
        base_url = supervisor.swap("b", health_timeout=10.0, poll_interval=0.1)
        assert base_url == f"http://127.0.0.1:{port_b}"
        assert supervisor.is_running
        assert not _port_in_use("127.0.0.1", port_a)
    finally:
        supervisor.stop()


def test_swap_while_already_stopped_just_starts(tmp_path, fake_bin):
    port = _free_port()
    config_path = _write_config(
        tmp_path, [{"name": "a", "gguf_path": "x", "ctx": 1, "n_gpu_layers": 0, "port": port}]
    )
    supervisor = LlamaSupervisor(config_path)
    try:
        base_url = supervisor.swap("a", health_timeout=10.0, poll_interval=0.1)
        assert base_url == f"http://127.0.0.1:{port}"
    finally:
        supervisor.stop()


# --- port-in-use / health-poll failure paths --------------------------------


def test_start_port_in_use_raises(tmp_path, fake_bin):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        config_path = _write_config(
            tmp_path, [{"name": "a", "gguf_path": "x", "ctx": 1, "n_gpu_layers": 0, "port": port}]
        )
        supervisor = LlamaSupervisor(config_path)
        with pytest.raises(LlamaSupervisorError):
            supervisor.start("a")
    finally:
        blocker.close()


def test_health_timeout_raises_and_kills_process(tmp_path, fake_bin):
    port = _free_port()
    config_path = _write_config(
        tmp_path,
        [
            {
                "name": "a",
                "gguf_path": "x",
                "ctx": 1,
                "n_gpu_layers": 0,
                "port": port,
                "args": ["--startup-delay", "5"],
            }
        ],
    )
    supervisor = LlamaSupervisor(config_path)
    with pytest.raises(LlamaSupervisorError):
        supervisor.start("a", health_timeout=1.0, poll_interval=0.1)
    assert not supervisor.is_running
    assert not _port_in_use("127.0.0.1", port)


def test_health_never_ok_raises(tmp_path, fake_bin):
    port = _free_port()
    config_path = _write_config(
        tmp_path,
        [
            {
                "name": "a",
                "gguf_path": "x",
                "ctx": 1,
                "n_gpu_layers": 0,
                "port": port,
                "args": ["--fail-health"],
            }
        ],
    )
    supervisor = LlamaSupervisor(config_path)
    with pytest.raises(LlamaSupervisorError):
        supervisor.start("a", health_timeout=1.0, poll_interval=0.1)
    assert not supervisor.is_running


def test_process_exits_early_during_health_poll_raises(tmp_path, fake_bin):
    port = _free_port()
    config_path = _write_config(
        tmp_path,
        [
            {
                "name": "a",
                "gguf_path": "x",
                "ctx": 1,
                "n_gpu_layers": 0,
                "port": port,
                "args": ["--crash-immediately"],
            }
        ],
    )
    supervisor = LlamaSupervisor(config_path)
    start_time = time.monotonic()
    with pytest.raises(LlamaSupervisorError, match=r"llama-server exited early \(code 17\)"):
        supervisor.start("a", health_timeout=10.0, poll_interval=0.1)
    elapsed = time.monotonic() - start_time
    assert elapsed < 5.0, f"expected fast-fail well under health_timeout, took {elapsed:.2f}s"
    assert not supervisor.is_running
    assert not _port_in_use("127.0.0.1", port)


def test_unresolved_binary_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("LLAMA_SERVER_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    config_path = _write_config(
        tmp_path, [{"name": "a", "gguf_path": "x", "ctx": 1, "n_gpu_layers": 0, "port": _free_port()}]
    )
    supervisor = LlamaSupervisor(config_path)
    with pytest.raises(LlamaSupervisorError):
        supervisor.start("a")


def test_env_bin_must_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("LLAMA_SERVER_BIN", str(tmp_path / "does-not-exist"))
    config_path = _write_config(
        tmp_path, [{"name": "a", "gguf_path": "x", "ctx": 1, "n_gpu_layers": 0, "port": _free_port()}]
    )
    supervisor = LlamaSupervisor(config_path)
    with pytest.raises(LlamaSupervisorError):
        supervisor.start("a")


# --- optional slow smoke test against a real llama-server -------------------


@pytest.mark.skipif(
    not os.environ.get("LLAMA_SERVER_BIN"),
    reason="LLAMA_SERVER_BIN not set; skipping real llama-server smoke test",
)
@pytest.mark.slow
def test_real_llama_server_smoke(tmp_path):
    port = _free_port()
    gguf_path = os.environ.get(
        "LLAMA_SMOKE_GGUF_PATH", "runtime/models/gemma-4-E2B-it-Q4_K_M.gguf"
    )
    if not os.path.isfile(gguf_path):
        pytest.skip(f"GGUF not found at {gguf_path}")
    config_path = _write_config(
        tmp_path,
        [{"name": "smoke", "gguf_path": gguf_path, "ctx": 2048, "n_gpu_layers": 999, "port": port}],
    )
    supervisor = LlamaSupervisor(config_path)
    try:
        base_url = supervisor.start("smoke", health_timeout=120.0, poll_interval=1.0)
        health = httpx.get(f"{base_url}/health", timeout=10.0)
        assert health.status_code == 200
        response = httpx.post(
            f"{base_url}/v1/chat/completions",
            json={"model": "smoke", "messages": [{"role": "user", "content": "Say hi."}]},
            timeout=60.0,
        )
        assert response.status_code == 200
    finally:
        supervisor.stop()
