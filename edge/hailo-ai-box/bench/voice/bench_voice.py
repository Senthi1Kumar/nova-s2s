"""Replay real WAV clips through the AI Box voice WebSocket and score the result."""

from __future__ import annotations

import argparse
import json
import os
import time
import wave
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from websockets.sync.client import connect

from .score import score_manifest, vad_warning

PROTOCOL = 2
FRAME_BYTES = 640  # 20 ms of 16 kHz mono PCM16
FRAME_S = 0.02
BULK_CHUNK = 32768
DEFAULT_URL = "http://127.0.0.1:8442"
STREAM_PATH = "/v1/voice/stream"


def read_pcm16_wav(path: str | Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
            raise ValueError(f"{path}: expected mono PCM16 at 16 kHz")
        return wav.readframes(wav.getnframes())


def stream_url(url: str) -> str:
    """http(s)://host[:port] to the matching ws(s) voice stream URL; ws URLs pass through."""
    parts = urlsplit(url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    path = parts.path if parts.path.strip("/") else STREAM_PATH
    return urlunsplit((scheme, parts.netloc, path, parts.query, ""))


class TurnRecorder:
    """Folds the server events of one turn into a result record.

    Times are seconds on any monotonic clock; latencies are measured from `stopped`.
    """

    def __init__(self, stopped: float) -> None:
        self.stopped = stopped
        self.transcript = ""
        self.reply = ""
        self.ignored: dict[str, Any] | None = None
        self.error: Any = None
        self.timings: dict[str, float] = {}
        self.finished = False
        self.vad: str | None = None
        self._announced: list[int] = []
        self._received: list[int] = []
        self._open = False

    def _since(self, now: float) -> float:
        return (now - self.stopped) * 1000

    def binary(self, size: int, now: float) -> None:
        self.timings.setdefault("first_audio_ms", self._since(now))
        if self._open:
            self._received[-1] += size

    def event(self, event: dict[str, Any], now: float) -> None:
        kind = event.get("type")
        vad = event.get("vad")
        if isinstance(vad, str) and self.vad != "failed":
            self.vad = vad
        if kind == "transcript_partial":
            self.timings.setdefault("first_partial_ms", self._since(now))
        elif kind == "transcript":
            self.transcript = event.get("text", "")
            self.timings.setdefault("asr_client_ms", self._since(now))
        elif kind == "token":
            self.timings.setdefault("llm_ttft_client_ms", self._since(now))
            self.reply += event.get("text", "")
        elif kind == "tts_start":
            self._announced.append(int(event.get("bytes", 0)))
            self._received.append(0)
            self._open = True
        elif kind == "tts_end":
            self._open = False
        elif kind == "done":
            self.reply = event.get("text", self.reply)
            self.timings["total_ms"] = self._since(now)
            for name, value in event.get("timings", {}).items():
                if isinstance(value, int | float):
                    self.timings[name] = value
            self.finished = True
        elif kind == "ignored":
            self.ignored = {key: event[key] for key in ("reason", "speech_ms", "ratio") if key in event}
            self.finished = True
        elif kind == "error":
            self.error = event.get("error", event)
            self.finished = True

    def record(self, clip: dict[str, Any], elapsed_ms: float) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": clip["id"],
            "scenario": clip.get("scenario"),
            "kind": clip.get("kind"),
            "condition": clip.get("condition"),
            "channel": clip.get("channel"),
            "transcript": self.transcript,
            "reply": self.reply,
            "ignored": self.ignored,
            "vad": self.vad,
            "timings": self.timings,
            "sentences": len(self._announced),
            "bytes_announced": sum(self._announced),
            "bytes_received": sum(self._received),
            "bytes_ok": self._received == self._announced,
            "elapsed_ms": elapsed_ms,
            "status": "ok",
        }
        if self.error is not None:
            result.update(status="error", error=str(self.error))
        elif not self.finished:
            result.update(status="error", error="timeout waiting for done/ignored")
        return result


def send_audio(ws: Any, audio: bytes, realtime: bool) -> None:
    if not realtime:
        for offset in range(0, len(audio), BULK_CHUNK):
            ws.send(audio[offset:offset + BULK_CHUNK])
        return
    begin = time.perf_counter()
    for index, offset in enumerate(range(0, len(audio), FRAME_BYTES)):
        delay = begin + index * FRAME_S - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        ws.send(audio[offset:offset + FRAME_BYTES])


def run_clip(
    url: str,
    clip: dict[str, Any],
    model: str,
    timeout_s: float,
    realtime: bool = False,
    api_key: str | None = None,
) -> dict[str, Any]:
    audio = read_pcm16_wav(clip["audio"])
    started = time.perf_counter()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    with connect(stream_url(url), open_timeout=10, close_timeout=1, additional_headers=headers) as ws:
        ws.send(json.dumps({"type": "start", "protocol": PROTOCOL, "model": model}))
        send_audio(ws, audio, realtime)
        stopped = time.perf_counter()
        ws.send(json.dumps({"type": "stop"}))
        turn = TurnRecorder(stopped)
        deadline = stopped + timeout_s
        while not turn.finished and time.perf_counter() < deadline:
            try:
                message = ws.recv(timeout=max(0.1, deadline - time.perf_counter()))
            except TimeoutError:
                break
            now = time.perf_counter()
            if isinstance(message, bytes):
                turn.binary(len(message), now)
            else:
                turn.event(json.loads(message), now)
    return turn.record(clip, (time.perf_counter() - started) * 1000)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--url", default=DEFAULT_URL, help="server base URL (ws:// or http://)")
    parser.add_argument("--api-key", default=os.environ.get("AIBOX_API_KEY"),
                        help="bearer key for LAN use (default $AIBOX_API_KEY); loopback needs none")
    parser.add_argument("--model", default="qwen3-vl")
    parser.add_argument("--timeout", type=float, default=120.0, help="timeout per clip in seconds")
    parser.add_argument("--realtime", action="store_true",
                        help="pace audio in 20 ms frames in real time (default: as fast as possible)")
    args = parser.parse_args()

    root = args.manifest.parent
    manifest = json.loads(args.manifest.read_text())
    clips: list[dict[str, Any]] = []
    for item in manifest.get("clips", []):
        clip = dict(item)
        clip["audio"] = str(root / (clip.get("path") or clip["audio"]))
        try:
            result = run_clip(args.url, clip, args.model, args.timeout, args.realtime, args.api_key)
        except Exception as exc:  # noqa: BLE001 - one bad clip must not discard the run
            result = {"id": clip["id"], "scenario": clip.get("scenario"), "kind": clip.get("kind"),
                      "condition": clip.get("condition"), "channel": clip.get("channel"),
                      "transcript": "", "status": "error", "error": str(exc), "timings": {}}
        clips.append(result)
        note = f"ignored:{result['ignored']['reason']}" if result.get("ignored") else repr(result.get("transcript", ""))
        print(f"{clip['id']}: {result['status']} {note}", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"clips": clips}, indent=2) + "\n")
    summary = score_manifest(args.manifest, args.output)
    args.output.write_text(json.dumps({"clips": clips, "summary": summary}, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    if warning := vad_warning(summary):
        print(warning)
    return 1 if any(clip["status"] == "error" for clip in clips) else 0


if __name__ == "__main__":
    raise SystemExit(main())
