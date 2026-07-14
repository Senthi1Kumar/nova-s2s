#!/usr/bin/env python3
"""Headless Realtime WS smoke test for the s2s pipeline (Task 1 spike).

Preflight-checks that (a) a llama-server (or any OpenAI-compatible chat
completions endpoint) is reachable, and (b) an s2s realtime WebSocket server
is reachable, failing fast with a clear message instead of hanging if either
is down. If both are up, drives one real round trip over
``ws://<host>:<port>/v1/realtime``:

  1. session.update (registers a dummy tool + instructions)
  2. stream tests/fixtures/asr/beckett_5s.wav as input_audio_buffer.append
  3. assert a transcript event and at least one response.output_audio.delta
     event come back
  4. mid-response, stream a second short audio burst to trigger VAD
     speech_started and assert the barge-in cancellation
     (response.done status=cancelled reason=turn_detected) is observed.

This script does not itself start the s2s pipeline process — see the
smoke checks for the s2s realtime path.
for the exact CLI invocation to start it manually pointed at your
llama-server. Auto-launching the pipeline (with STT/TTS model downloads) is
out of scope for this spike; that's owned by the Task 2 launcher.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import wave
from pathlib import Path

import httpx
import websockets

DEFAULT_WAV = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "asr" / "beckett_5s.wav"
CHUNK_BYTES = 3200  # 100ms of 16kHz mono PCM16
CONNECT_TIMEOUT_S = 3.0
EVENT_TIMEOUT_S = 30.0
BARGE_IN_TIMEOUT_S = 15.0

DUMMY_TOOL = {
    "type": "function",
    "name": "get_weather",
    "description": "Get the current weather for a city.",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name."}},
        "required": ["city"],
    },
}


def _fail(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def check_llama_server(base_url: str) -> None:
    """Fail fast (not hang) if the OpenAI-compatible LLM endpoint is down."""
    url = base_url.rstrip("/") + "/models"
    try:
        resp = httpx.get(url, timeout=CONNECT_TIMEOUT_S)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        _fail(
            f"llama-server not reachable at {base_url} ({exc.__class__.__name__}: {exc}). "
            f"Start llama-server with an OpenAI-compatible endpoint at {base_url} and retry."
        )
    print(f"OK: llama-server reachable at {base_url}")


def _start_hint(host: str, port: int, llama_url: str) -> str:
    return (
        "Start it manually, e.g.:\n"
        "  uv run python -m speech_to_speech.s2s_pipeline \\\n"
        "    --mode realtime --llm_backend chat-completions --tts kokoro \\\n"
        f"    --responses_api_base_url {llama_url!r} \\\n"
        "    --model_name <your-gguf-model-name> \\\n"
        f"    --ws_host {host} --ws_port {port}"
    )


async def _check_ws_handshake(host: str, port: int) -> None:
    """Attempt the actual /v1/realtime WS handshake (not just a TCP connect).

    A bare TCP connect isn't enough: an unrelated process can be squatting on
    the port (observed in this environment — a leftover process from a
    different repo happened to hold 8765), which a plain socket check can't
    tell apart from a real s2s server.
    """
    async with websockets.connect(f"ws://{host}:{port}/v1/realtime", open_timeout=CONNECT_TIMEOUT_S):
        pass


def check_ws_server(host: str, port: int, llama_url: str) -> None:
    """Fail fast (not hang) if the s2s realtime server is down or the port is
    held by something else."""
    try:
        asyncio.run(asyncio.wait_for(_check_ws_handshake(host, port), timeout=CONNECT_TIMEOUT_S))
    except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as exc:
        _fail(
            f"s2s realtime server not reachable at ws://{host}:{port}/v1/realtime "
            f"({exc.__class__.__name__}: {exc}). {_start_hint(host, port, llama_url)}"
        )
    print(f"OK: s2s realtime server reachable at ws://{host}:{port}/v1/realtime")


def load_pcm16(wav_path: Path) -> bytes:
    with wave.open(str(wav_path), "rb") as wf:
        if wf.getframerate() != 16000 or wf.getsampwidth() != 2 or wf.getnchannels() != 1:
            _fail(
                f"{wav_path} is not 16kHz mono PCM16 "
                f"(got rate={wf.getframerate()}, width={wf.getsampwidth()}, ch={wf.getnchannels()})"
            )
        return wf.readframes(wf.getnframes())


async def _send_audio(ws: "websockets.ClientConnection", pcm: bytes) -> None:
    for i in range(0, len(pcm), CHUNK_BYTES):
        chunk = pcm[i : i + CHUNK_BYTES]
        await ws.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                }
            )
        )
        await asyncio.sleep(0.0)  # yield, no real-time pacing needed


async def run_round_trip(host: str, port: int, wav_path: Path) -> bool:
    pcm = load_pcm16(wav_path)
    uri = f"ws://{host}:{port}/v1/realtime"

    seen_types: list[str] = []
    got_transcript = False
    got_audio_delta = False
    got_barge_in_cancel = False

    async with websockets.connect(uri, open_timeout=CONNECT_TIMEOUT_S) as ws:
        created = json.loads(await asyncio.wait_for(ws.recv(), timeout=EVENT_TIMEOUT_S))
        print(f"<- {created['type']}")
        assert created["type"] == "session.created", created

        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "instructions": "You are Nova, a helpful in-vehicle voice assistant.",
                        "tools": [DUMMY_TOOL],
                    },
                }
            )
        )

        await _send_audio(ws, pcm)

        barge_in_sent = False
        deadline = asyncio.get_event_loop().time() + EVENT_TIMEOUT_S
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.1))
            except asyncio.TimeoutError:
                break
            event = json.loads(raw)
            seen_types.append(event["type"])
            print(f"<- {event['type']}")

            if event["type"] == "conversation.item.input_audio_transcription.completed":
                got_transcript = True
            if event["type"] == "response.output_audio.delta":
                got_audio_delta = True
                if not barge_in_sent:
                    # Mid-response: stream a second burst to trigger VAD
                    # speech_started -> barge-in cancellation.
                    barge_in_sent = True
                    deadline = asyncio.get_event_loop().time() + BARGE_IN_TIMEOUT_S
                    asyncio.create_task(_send_audio(ws, pcm[: CHUNK_BYTES * 20]))
            if (
                event["type"] == "response.done"
                and event.get("response", {}).get("status") == "cancelled"
                and event.get("response", {}).get("status_details", {}).get("reason") == "turn_detected"
            ):
                got_barge_in_cancel = True

            if got_transcript and got_audio_delta and got_barge_in_cancel:
                break

    print("\n--- Summary ---")
    print(f"Event types seen: {seen_types}")
    print(f"Transcript event:      {'PASS' if got_transcript else 'FAIL'}")
    print(f"Audio-delta event:     {'PASS' if got_audio_delta else 'FAIL'}")
    print(f"Barge-in cancellation: {'PASS' if got_barge_in_cancel else 'FAIL'}")
    return got_transcript and got_audio_delta and got_barge_in_cancel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llama-url", default="http://localhost:8080/v1", help="OpenAI-compatible llama-server base URL")
    parser.add_argument("--ws-host", default="127.0.0.1")
    parser.add_argument("--ws-port", type=int, default=8765)
    parser.add_argument("--wav", type=Path, default=DEFAULT_WAV)
    args = parser.parse_args()

    check_llama_server(args.llama_url)
    check_ws_server(args.ws_host, args.ws_port, args.llama_url)

    ok = asyncio.run(run_round_trip(args.ws_host, args.ws_port, args.wav))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
