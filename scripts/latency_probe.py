"""Cold-turn latency probe against a running Nova stack.

Start the stack first (scripts/run_demo.py), then:
    uv run python scripts/latency_probe.py path/to/utterance.wav

Connects to the s2s realtime WS, streams the wav as 16k PCM16, and records
wall-clock from audio-end to (a) transcript, (b) first response.output_audio.delta.
"""
import asyncio, base64, json, sys, time, wave
import websockets

WS = "ws://127.0.0.1:8766/v1/realtime"


async def main(wav_path: str) -> None:
    with wave.open(wav_path, "rb") as w:
        assert w.getframerate() == 16000 and w.getsampwidth() == 2
        pcm = w.readframes(w.getnframes())
    async with websockets.connect(WS, max_size=None) as ws:
        await ws.send(json.dumps({"type": "session.update",
            "session": {"type": "realtime", "instructions": "You are Nova.",
                        "audio": {"input": {"turn_detection": {"type": "server_vad"}}}}}))
        chunk = 3200  # 100ms @16k PCM16
        for i in range(0, len(pcm), chunk):
            await ws.send(json.dumps({"type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm[i:i+chunk]).decode()}))
            await asyncio.sleep(0.01)
        t_audio_end = time.monotonic()
        t_stt = t_ttfb = None
        while t_ttfb is None:
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            typ = ev.get("type", "")
            if "transcription" in typ and t_stt is None:
                t_stt = time.monotonic() - t_audio_end
            if typ == "response.output_audio.delta":
                t_ttfb = time.monotonic() - t_audio_end
        print(f"STT: {t_stt:.2f}s  TTFB: {t_ttfb:.2f}s")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
