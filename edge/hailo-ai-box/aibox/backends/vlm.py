"""Qwen-VL in a dedicated worker process, serialized through the shared GenAI slot."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import threading
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np

from ..config import Settings
from ..services import ChatService, VlmService
from .genai import GenAISlot
from .ollama import OllamaChat
from .registry import Registry

VLM_MODELS = ("qwen3-vl", "qwen2-vl")
VLM_MAX_TOKENS = 160
SYSTEM = "You are a concise, accurate visual assistant."
_END = object()


def vlm_hef(settings: Settings, model: str) -> str:
    if model == "qwen3-vl":
        return settings.qwen3_vl_hef
    if model == "qwen2-vl":
        return os.path.join(settings.hef_dir, "Qwen2-VL-2B-Instruct.hef")
    raise ValueError(f"unknown VLM {model!r}")


def fit_frame(img: np.ndarray, shape: tuple[int, ...], dtype: Any = np.uint8) -> np.ndarray:
    """Letterbox `img` (HxWx3 RGB) into the VLM's (h, w[, 3]) frame; always a fresh writable array."""
    import cv2

    h, w = int(shape[0]), int(shape[1])
    ih, iw = img.shape[:2]
    s = min(w / iw, h / ih)
    nw, nh = max(1, round(iw * s)), max(1, round(ih * s))
    resized = cv2.resize(np.ascontiguousarray(img), (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.zeros((h, w, 3), dtype=np.uint8)
    y0, x0 = (h - nh) // 2, (w - nw) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = resized
    return np.array(canvas, dtype=dtype, order="C")


def build_prompt(messages: list[dict], with_image: bool) -> list[dict]:
    """OpenAI-style messages to Hailo VLM prompt; the image goes on the last user turn."""
    out: list[dict] = []
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    if not any(m.get("role") == "system" for m in messages):
        out.append({"role": "system", "content": [{"type": "text", "text": SYSTEM}]})
    for i, m in enumerate(messages):
        c = m.get("content")
        if isinstance(c, str):
            text = c
        else:
            text = "".join(p.get("text", "") for p in (c or []) if isinstance(p, dict)
                           and p.get("type") == "text")
        parts: list[dict] = []
        if with_image and i == last_user:
            parts.append({"type": "image"})
        parts.append({"type": "text", "text": text})
        out.append({"role": m.get("role", "user"), "content": parts})
    return out


class _VlmHandle:
    """A running worker process holding the VLM; `unload()` ends the process (frees device RAM)."""

    def __init__(self, owner: HailoVlm, model: str, proc: Any, conn: Any, shape: tuple,
                 dtype: str) -> None:
        self.owner, self.model, self.proc, self.conn = owner, model, proc, conn
        self.shape, self.dtype = shape, dtype

    async def unload(self) -> None:
        self.owner._set(self.model, "evicting")
        await asyncio.to_thread(self._stop)
        self.owner._set(self.model, "unloaded")

    def _stop(self) -> None:
        try:
            self.conn.send(("quit",))
        except (OSError, BrokenPipeError):
            pass
        self.proc.join(15)
        if self.proc.is_alive():
            self.proc.kill()
            self.proc.join(5)
        try:
            self.conn.close()
        except OSError:
            pass
        # the chip keeps the VLM's weights until this process's VDevice is recycled
        hook = self.owner.after_unload
        if hook is not None:
            hook()

    def generate(self, prompt: list, frames: list, max_tokens: int, stop: threading.Event):
        """Blocking generator of text chunks (call from a worker thread)."""
        self.conn.send(("gen", prompt, frames, max_tokens))
        stopped = False
        while True:
            if stop.is_set() and not stopped:
                stopped = True
                try:
                    self.conn.send(("stop",))
                except (OSError, BrokenPipeError):
                    return
            if not self.conn.poll(0.05):
                if not self.proc.is_alive():
                    raise RuntimeError("VLM worker process died")
                continue
            msg = self.conn.recv()
            if msg[0] == "tok":
                if not stopped:
                    yield msg[1]
            elif msg[0] == "done":
                return
            elif msg[0] == "error":
                raise RuntimeError(msg[1])


class HailoVlm(VlmService, ChatService):
    def __init__(self, slot: GenAISlot, pool: Any, settings: Settings,
                 registry: Registry | None = None) -> None:
        self.slot, self.pool, self.settings, self.registry = slot, pool, settings, registry
        self._exec = ThreadPoolExecutor(1, thread_name_prefix="hailo-vlm")
        self.on_resource_exhausted: Any = None  # sync callable: free NPU resources, return bool
        self.after_unload: Any = None  # sync callable run after a VLM worker exits (recycle)

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._exec, fn, *args)

    def _spawn(self, model: str) -> _VlmHandle:
        path = vlm_hef(self.settings, model)
        if not os.path.exists(path):
            raise FileNotFoundError(f"VLM HEF not found: {path}")
        from .vlm_worker import worker_main

        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe()
        proc = ctx.Process(target=worker_main, args=(child, path), daemon=True, name="vlm-worker")
        proc.start()
        child.close()
        deadline = time.monotonic() + 180
        while not parent.poll(0.2):
            if not proc.is_alive() or time.monotonic() > deadline:
                proc.kill()
                raise RuntimeError("VLM worker did not start")
        msg = parent.recv()
        if msg[0] != "ready":
            proc.join(10)
            raise RuntimeError(msg[1])
        return _VlmHandle(self, model, proc, parent, msg[1], msg[2])

    def loader(self, model: str):
        async def load() -> _VlmHandle:
            t0 = time.monotonic()
            self._set(model, "loading")
            try:
                while True:
                    try:
                        handle = await self._run(self._spawn, model)
                        break
                    except Exception as first:
                        if isinstance(first, (FileNotFoundError, ValueError)):
                            raise
                        hook = self.on_resource_exhausted
                        # A worker can die before returning the HailoRT error. Evict and recycle
                        # on each bounded retry instead of requiring a particular error string.
                        if hook is None or not await self._run(hook):
                            raise
                        if self.after_unload is not None:
                            await self._run(self.after_unload)
            except Exception as exc:
                self._set(model, "failed", f"{type(exc).__name__}: {exc}")
                raise
            if self.registry is not None:
                self.registry.set_state(model, "resident", load_s=time.monotonic() - t0)
            return handle

        return load

    def _set(self, model: str, state: str, error: str | None = None) -> None:
        if self.registry is not None:
            self.registry.set_state(model, state, error=error)

    async def stream(self, model: str, messages: list[dict], max_tokens: int,
                     images: list[np.ndarray] | None = None) -> AsyncIterator[str]:
        if model not in VLM_MODELS:
            model = "qwen3-vl"
        max_tokens = min(int(max_tokens), VLM_MAX_TOKENS)
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        t0 = time.perf_counter()
        async with self.slot.acquire(f"vlm:{model}") as handle:
            prompt = build_prompt(messages, bool(images))
            frames = []
            if images:
                dtype = np.float32 if "float" in handle.dtype.lower() else np.uint8
                frames = [fit_frame(images[0], handle.shape, dtype)]

            def work() -> None:
                try:
                    for chunk in handle.generate(prompt, frames, max_tokens, stop):
                        text = chunk.replace("<|im_end|>", "")
                        if text:
                            loop.call_soon_threadsafe(q.put_nowait, text)
                except Exception as exc:  # noqa: BLE001
                    loop.call_soon_threadsafe(q.put_nowait, exc)
                finally:
                    loop.call_soon_threadsafe(q.put_nowait, _END)

            fut = loop.run_in_executor(self._exec, work)
            try:
                while True:
                    item = await q.get()
                    if item is _END:
                        break
                    if isinstance(item, Exception):
                        raise item
                    yield item
            finally:
                stop.set()
                # hold the slot until the worker has really stopped generating
                await asyncio.shield(fut)
        if self.registry is not None:
            self.registry.record_call(model, (time.perf_counter() - t0) * 1000.0)

    async def ask(self, image: np.ndarray, prompt: str, model: str = "qwen3-vl",
                  max_tokens: int = VLM_MAX_TOKENS) -> str:
        parts = []
        async for t in self.stream(model, [{"role": "user", "content": prompt}], max_tokens,
                                   images=[image]):
            parts.append(t)
        return "".join(parts).strip()

    async def aclose(self) -> None:
        self._exec.shutdown(wait=False)


class RoutingChat(ChatService):
    """VLM ids or any request with images go to HailoVlm; everything else to hailo-ollama."""

    def __init__(self, vlm: HailoVlm, ollama: OllamaChat) -> None:
        self.vlm, self.ollama = vlm, ollama

    def stream(self, model: str, messages: list[dict], max_tokens: int,
               images: list[np.ndarray] | None = None) -> AsyncIterator[str]:
        if model in VLM_MODELS or images:
            return self.vlm.stream(model, messages, max_tokens, images)
        return self.ollama.stream(model, messages, max_tokens, images)


def build_genai(emit: Any, pool: Any, settings: Settings, registry: Registry | None = None):
    """Wire slot + HailoVlm + OllamaChat + RoutingChat. Returns (slot, vlm, ollama, routing)."""

    def loader_for(key: str):
        kind, _, model = key.partition(":")
        if kind == "vlm":
            return vlm.loader(model)
        if kind == "ollama":
            return ollama.loader(model)
        raise ValueError(f"bad GenAI owner key {key!r}")

    slot = GenAISlot(emit, loader_for)
    vlm = HailoVlm(slot, pool, settings, registry)
    log_path = os.path.join(os.getcwd(), "runtime", "logs", "ollama.log")
    ollama = OllamaChat(slot, settings.ollama_url, registry, pool=pool,
                        log_path=log_path if os.path.isdir(os.path.dirname(log_path)) else None)
    return slot, vlm, ollama, RoutingChat(vlm, ollama)
