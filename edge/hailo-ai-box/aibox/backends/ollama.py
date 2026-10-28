"""hailo-ollama chat (streaming NDJSON over httpx) behind the GenAI slot."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import numpy as np

from ..services import ChatService
from .genai import GenAISlot
from .registry import Registry

# hailo-ollama rejects the string form ("10m": 500 "Value is NOT a Primitive type"); 600 s == 10 m
KEEP_ALIVE = 600
CHAT_MAX_TOKENS = 512


def ollama_hef_path(model: str) -> str | None:
    """Resolve a chat model's local HEF through hailo-ollama's versioned manifest."""
    family, sep, tag = model.partition(":")
    if not sep or not family or not tag:
        return None
    manifest_root = os.environ.get(
        "HAILO_OLLAMA_MANIFESTS", "/usr/share/hailo-ollama/models/manifests")
    manifest = os.path.join(manifest_root, family, tag, "manifest.json")
    try:
        with open(manifest, encoding="utf-8") as fh:
            digest = json.load(fh).get("hef_h10h")
    except (OSError, ValueError):
        return None
    if not digest:
        return None
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(data_home, "hailo-ollama", "models", "blob", f"sha256_{digest}")
    return path if os.path.isfile(path) else None


def _find_pids(comm: str) -> list[int]:
    pids = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/comm") as f:
                    if f.read().strip() == comm:
                        pids.append(int(d))
            except OSError:
                continue
    return pids


def restart_hailo_ollama(base_url: str, log_path: str | None = None, wait_s: float = 20.0) -> bool:
    """hailo-ollama keeps a model's weights on the chip after keep_alive 0; only a process restart
    returns that device RAM. Kill every hailo-ollama and start one with the SHARED vdevice group."""
    for pid in _find_pids("hailo-ollama"):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 8
    while _find_pids("hailo-ollama") and time.monotonic() < deadline:
        time.sleep(0.1)
    for pid in _find_pids("hailo-ollama"):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    exe = shutil.which("hailo-ollama")
    if exe is None:
        return False
    env = dict(os.environ, HAILO_OLLAMA_VDEVICE_GROUP_ID="SHARED")
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL
    subprocess.Popen([exe], env=env, stdout=out, stderr=subprocess.STDOUT,  # noqa: S603
                     stdin=subprocess.DEVNULL, start_new_session=True, cwd=os.path.expanduser("~"))
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(base_url + "/api/tags", timeout=1.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


class _OllamaHandle:
    def __init__(self, chat: OllamaChat, model: str) -> None:
        self.chat, self.model = chat, model

    async def unload(self) -> None:
        await self.chat.unload(self.model)


class OllamaChat(ChatService):
    on_resource_exhausted: Any = None  # async (model) -> bool: evict vision to make room
    def __init__(self, slot: GenAISlot, base_url: str, registry: Registry | None = None,
                 pool: Any = None, log_path: str | None = None) -> None:
        self.pool, self.log_path = pool, log_path
        self.after_unload: Any = None  # sync callable: recycle the NPU VDevice
        self.slot = slot
        self.base_url = base_url.rstrip("/")
        self.registry = registry
        self._client = httpx.AsyncClient(base_url=self.base_url,
                                         timeout=httpx.Timeout(120.0, connect=5.0))

    def loader(self, model: str):
        """Slot loader for `ollama:<model>`: load the model into the KV slot."""

        async def load() -> _OllamaHandle:
            t0 = time.monotonic()
            self._set(model, "loading")
            # async hook(model) -> bool: free device RAM (evict one vision group); True if it did
            hook = self.on_resource_exhausted
            while True:
                try:
                    await self._load_once(model)
                    break
                except Exception as exc:
                    exhausted = hook is not None and _is_exhausted(exc)
                    if exhausted and await hook(model):
                        continue
                    if self.registry is not None:
                        self.registry.set_state(model, "failed",
                                                error=f"{type(exc).__name__}: {exc}")
                    if exhausted:
                        raise RuntimeError(
                            f"no free NPU resources to load {model} after evicting vision "
                            f"models ({exc})") from exc
                    raise
            if self.registry is not None:
                self.registry.set_state(model, "resident", load_s=time.monotonic() - t0)
            return _OllamaHandle(self, model)

        return load

    async def _load_once(self, model: str) -> None:
        # an empty /api/generate prompt free-runs; a 1-token chat loads the model cleanly
        r = await self._client.post("/api/chat", json={
            "model": model, "messages": [{"role": "user", "content": "hi"}],
            "stream": False, "keep_alive": KEEP_ALIVE, "options": {"num_predict": 1}})
        r.raise_for_status()

    def _set(self, model: str, state: str) -> None:
        if self.registry is not None:
            try:
                self.registry.set_state(model, state)
            except KeyError:
                pass

    async def unload(self, model: str) -> None:
        self._set(model, "evicting")
        try:
            await self._client.post("/api/generate", json={"model": model, "keep_alive": 0})
        except httpx.HTTPError:
            pass  # ollama already gone; the slot is free either way
        self._set(model, "unloaded")
        await self._reclaim_device_ram()

    async def _reclaim_device_ram(self) -> None:
        """Recycle once: unloaded Ollama weights can remain allocated despite the reported free RAM."""
        if self.pool is None:
            return
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        if self.after_unload is not None:
            await asyncio.to_thread(self.after_unload)

        def free_mb() -> float:
            st = self.pool.stats()
            return st["ram_total_mb"] - st["ram_used_mb"] if st["ram_total_mb"] else 1e9

        free = await asyncio.to_thread(free_mb)
        self.slot.emit("npu_reclaim", free_mb=round(free), ms=round((time.monotonic() - t0) * 1000))

    async def list_models(self) -> list[str]:
        r = await self._client.get("/api/tags")
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    async def alive(self) -> bool:
        try:
            return (await self._client.get("/api/tags", timeout=2.0)).status_code == 200
        except httpx.HTTPError:
            return False

    async def stream(self, model: str, messages: list[dict], max_tokens: int,
                     images: list[np.ndarray] | None = None) -> AsyncIterator[str]:
        body = {
            "model": model, "messages": _text_messages(messages), "stream": True,
            "keep_alive": KEEP_ALIVE,
            "options": {"num_predict": min(int(max_tokens), CHAT_MAX_TOKENS)},
        }
        t0 = time.perf_counter()
        async with self.slot.acquire(f"ollama:{model}"):
            async with self._client.stream("POST", "/api/chat", json=body) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                    raise RuntimeError(f"hailo-ollama {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    if msg.get("error"):
                        raise RuntimeError(f"hailo-ollama: {msg['error']}")
                    text = (msg.get("message") or {}).get("content") or ""
                    if text:
                        yield text
                    if msg.get("done"):
                        break
        if self.registry is not None:
            self.registry.record_call(model, (time.perf_counter() - t0) * 1000.0)

    async def aclose(self) -> None:
        await self._client.aclose()


def _is_exhausted(exc: Exception) -> bool:
    t = str(exc)
    # hailo-ollama reports a failed load as a bare HTTP 500 (details only in its own log)
    return ("RESOURCE_EXHAUSTED" in t or "Failed to create LLM" in t or "(81)" in t
            or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 500))


def _text_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = "".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        out.append({"role": m.get("role", "user"), "content": c or ""})
    return out
