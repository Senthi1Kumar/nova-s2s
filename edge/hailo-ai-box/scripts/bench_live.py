#!/usr/bin/env python3
"""Repeatable live API smoke/performance check for a running AI Box server."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx
import urllib3

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "data"


class Bench:
    def __init__(self, base: str, key: str | None) -> None:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.client = httpx.Client(base_url=base, headers=headers, verify=False, timeout=120)
        self.rows: list[dict] = []

    def close(self) -> None:
        self.client.close()

    def record(self, name: str, started: float, response: httpx.Response, **extra) -> dict:
        row = {"check": name, "status": response.status_code,
               "total_s": round(time.perf_counter() - started, 3), **extra}
        self.rows.append(row)
        print(json.dumps(row), flush=True)
        response.raise_for_status()
        return row

    def detect(self, task: str, image: str) -> None:
        for attempt in range(2):
            started = time.perf_counter()
            with (DATA / image).open("rb") as fh:
                response = self.client.post("/v1/vision/detect", data={"task": task},
                                            files={"image": (image, fh, "image/png")})
            body = response.json()
            self.record(f"vision:{task}:{'warm' if attempt else 'load'}", started, response,
                        inference_ms=body.get("inference_ms"))
        if task == "detection":
            assert any(d.get("class") == "person" for d in body["detections"]), body
        elif task == "ocr":
            assert any("RACQUET" in line["text"].upper() for line in body["text"]), body
        elif task == "lanes":
            assert body["lanes"], body
        elif task == "dms":
            assert body["dms"]["faces"] >= 1, body
        else:
            assert body["detections"], body

    def chat(self, model: str = "qwen3-vl", label: str = "chat:warm") -> None:
        started = time.perf_counter()
        first = None
        text = []
        with self.client.stream("POST", "/v1/chat/completions", json={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: READY"}],
            "max_tokens": 12,
            "stream": True,
        }) as response:
            status = response.status_code
            for line in response.iter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                event = json.loads(line[6:])
                chunk = event.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if chunk:
                    if first is None:
                        first = time.perf_counter() - started
                    text.append(chunk)
        row = self.record(label, started, response, ttft_s=round(first or -1, 3),
                          text="".join(text), model=model)
        assert status == 200 and "READY" in row["text"].upper(), row

    def vlm(self) -> None:
        started = time.perf_counter()
        with (DATA / "sample_face.png").open("rb") as fh:
            response = self.client.post("/v1/vision/ask", data={
                "prompt": "What word is printed on the shirt? Answer with one word.",
                "model": "qwen3-vl", "max_tokens": 24,
            }, files={"image": ("sample_face.png", fh, "image/png")})
        body = response.json()
        self.record("vlm:ocr", started, response, answer=body.get("answer"))
        assert "RACQ" in body["answer"].upper(), body

    def audio(self) -> None:
        started = time.perf_counter()
        response = self.client.post("/v1/audio/speech", json={
            "input": "Hello from the AI box.", "response_format": "wav"})
        wav = response.content
        self.record("tts", started, response, wav_bytes=len(wav),
                    inference_ms=float(response.headers.get("X-Infer-Ms", 0)))
        assert wav.startswith(b"RIFF")
        started = time.perf_counter()
        response = self.client.post("/v1/audio/transcriptions",
                                    files={"file": ("reply.wav", wav, "audio/wav")})
        body = response.json()
        self.record("asr", started, response, text=body.get("text"), audio_s=body.get("audio_s"),
                    inference_ms=(body.get("meta") or {}).get("infer_ms"))
        words = {word.strip(".,!?").lower() for word in body["text"].split()}
        assert {"hello", "box"} <= words, body


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("AIBOX_URL", "https://127.0.0.1:8443"))
    parser.add_argument("--swaps", action="store_true", help="also measure deliberate Ollama/VLM swaps")
    parser.add_argument("--output", help="optional JSON report path")
    args = parser.parse_args()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    bench = Bench(args.url.rstrip("/"), os.environ.get("AIBOX_API_KEY") or None)
    try:
        health = bench.client.get("/health")
        health.raise_for_status()
        for task, image in (("detection", "sample_face.png"), ("pose", "sample_face.png"),
                            ("segmentation", "sample_face.png"), ("lanes", "c920e.png"),
                            ("dms", "sample_face.png"), ("ocr", "sample_face.png")):
            bench.detect(task, image)
        bench.vlm()
        bench.chat()
        bench.chat(label="chat:warm:second")
        bench.audio()
        if args.swaps:
            bench.chat("qwen2.5:1.5b", "swap:vlm_to_ollama")
            bench.chat("qwen2.5:1.5b", "chat:ollama_warm")
            bench.detect("lanes", "c920e.png")
            bench.chat("qwen3-vl", "swap:lanes_to_vlm")
        report = {"url": args.url, "status": "ok", "checks": bench.rows}
    finally:
        bench.close()
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": "ok", "checks": len(bench.rows)}))


if __name__ == "__main__":
    main()
