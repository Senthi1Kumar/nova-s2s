import asyncio
import io
import json
import time
import wave

import numpy as np
import pytest
from websockets.sync.client import connect

from aibox.voice.pipeline import SentenceBuffer
from tests.test_api_inference import live  # noqa: F401


def test_sentence_buffer_streams_complete_sentences_and_flushes_tail():
    buf = SentenceBuffer()
    assert buf.feed("First sentence. Second") == ["First sentence."]
    assert buf.feed(" sentence!") == []
    assert buf.flush() == "Second sentence!"


def test_sentence_buffer_does_not_split_decimals():
    buf = SentenceBuffer()
    out = []
    for token in ["Set ", "it ", "to ", "21.", "5 ", "degrees."]:
        out += buf.feed(token)
    assert out == []
    assert buf.flush() == "Set it to 21.5 degrees."


def test_sentence_buffer_emits_a_clause_before_the_sentence_ends():
    buf = SentenceBuffer()
    assert buf.feed("I can help with that, ") == ["I can help with that,"]
    assert buf.feed("and then we can continue") == []
    assert buf.flush() == "and then we can continue"


def test_sentence_buffer_emits_eight_complete_words_without_punctuation():
    buf = SentenceBuffer()
    assert buf.feed("one two three four five six seven eight") == []
    assert buf.feed(" nine ten") == ["one two three four five six seven eight"]
    assert buf.flush() == "nine ten"


def test_sentence_buffer_drops_punctuation_only_input():
    buf = SentenceBuffer()
    assert buf.feed(". ") == []
    assert buf.feed("!") == []
    assert buf.flush() == ""


def _json(message):
    return json.loads(message) if isinstance(message, str) else None


def _run_turn(ws, pcm, turn_id, wait_partial=False):
    ws.send(json.dumps({"type": "start", "protocol": 2, "model": "qwen3-vl"}))
    ws.send(pcm)
    if wait_partial:
        time.sleep(0.8)
    ws.send(json.dumps({"type": "stop"}))
    events = []
    audio = 0
    deadline = time.time() + 10
    while time.time() < deadline:
        raw = ws.recv(timeout=10)
        if isinstance(raw, bytes):
            audio += 1
            events.append(("audio", len(raw)))
            continue
        event = _json(raw)
        events.append((event["type"], event))
        if event["type"] == "done":
            break
    return events, audio


class TwoSentenceChat:
    def __init__(self):
        self.calls = 0
        self.cancelled = False

    async def stream(self, model, messages, max_tokens, images=None):
        self.calls += 1
        try:
            yield "First sentence. "
            await asyncio.sleep(0.15)
            yield "Second sentence!"
        finally:
            pass


def test_protocol_two_streams_partials_and_sentence_audio_before_done(live, services):  # noqa: F811
    services.chat = TwoSentenceChat()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        events, audio = _run_turn(ws, np.zeros(16000, "<i2").tobytes(), 1, wait_partial=True)
    kinds = [kind for kind, _ in events]
    assert "transcript_partial" in kinds
    assert kinds.index("transcript_partial") < kinds.index("transcript")
    assert kinds.index("tts_start") < kinds.index("done")
    starts = [event for kind, event in events if kind == "tts_start"]
    assert [event["sentence_seq"] for event in starts] == [0, 1]
    assert all(event["turn_id"] == starts[0]["turn_id"] for event in starts)
    assert audio >= 2
    last_start = max(i for i, (kind, _) in enumerate(events) if kind == "tts_start")
    done = kinds.index("done")
    assert any(kind == "audio" for kind, _ in events[last_start:done]), "done overtook the last sentence audio"


def test_protocol_two_manual_follow_up_gets_new_turn_id(live, services):  # noqa: F811
    services.chat = TwoSentenceChat()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        first, _ = _run_turn(ws, np.zeros(8000, "<i2").tobytes(), 1)
        second, _ = _run_turn(ws, np.zeros(8000, "<i2").tobytes(), 2)
    first_id = next(event["turn_id"] for kind, event in first if kind == "transcript")
    second_id = next(event["turn_id"] for kind, event in second if kind == "transcript")
    assert second_id != first_id


class EmptyAsr:
    async def transcribe(self, pcm16k, language=None):
        return ""


def test_protocol_two_empty_transcript_does_not_call_llm(live, services):  # noqa: F811
    services.asr = EmptyAsr()
    services.chat = TwoSentenceChat()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        events, audio = _run_turn(ws, np.zeros(8000, "<i2").tobytes(), 1)
    assert any(kind == "done" for kind, _ in events)
    assert services.chat.calls == 0
    assert audio == 0


def _wav(samples: int, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * samples)
    return buf.getvalue()


class FixedTts:
    def __init__(self, samples=3001, rate=16000, delay=0.0):
        self.samples, self.rate, self.delay = samples, rate, delay

    async def synthesize(self, text, voice=None) -> bytes:
        await asyncio.sleep(self.delay)
        return _wav(self.samples, self.rate)


class ThreeSentenceChat:
    async def stream(self, model, messages, max_tokens, images=None):
        for part in ["One. ", "Two. ", "Three. "]:
            yield part
            await asyncio.sleep(0.02)


def _collect(ws, pcm):
    """Run one turn; return the ordered list of ("bin", len) / ("json", event)."""
    ws.send(json.dumps({"type": "start", "protocol": 2}))
    ws.send(pcm)
    ws.send(json.dumps({"type": "stop"}))
    log = []
    deadline = time.time() + 15
    while time.time() < deadline:
        raw = ws.recv(timeout=10)
        if isinstance(raw, bytes):
            assert raw[:4] != b"RIFF"
            log.append(("bin", raw))
            continue
        event = json.loads(raw)
        log.append(("json", event))
        if event["type"] == "done":
            break
    try:
        while True:
            raw = ws.recv(timeout=0.3)
            log.append(("bin" if isinstance(raw, bytes) else "json", raw))
    except TimeoutError:
        pass
    return log


def _assert_exact_ordering(log):
    open_seq = None
    sent = 0
    ends = starts = 0
    done_seen = False
    for kind, item in log:
        assert not done_seen, "message after done"
        if kind == "bin":
            assert open_seq is not None, "binary outside a sentence"
            sent += len(item)
            continue
        t = item["type"]
        if t == "tts_start":
            assert open_seq is None
            open_seq, sent = item["sentence_seq"], 0
            assert item["encoding"] == "pcm16" and item["bytes"] % 1280 == 0
            expected = item["bytes"]
            starts += 1
        elif t == "tts_end":
            assert open_seq == item["sentence_seq"]
            assert sent == expected == item["bytes"]
            open_seq = None
            ends += 1
        elif t == "done":
            assert open_seq is None and starts == ends
            done_seen = True
    assert done_seen
    return starts


@pytest.mark.parametrize("tts", [
    FixedTts(3001),
    FixedTts(2000, rate=22050),
    FixedTts(1),
])
def test_sentence_bytes_match_tts_start(live, services, tts):  # noqa: F811
    services.chat = ThreeSentenceChat()
    services.tts = tts
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        log = _collect(ws, np.zeros(8000, "<i2").tobytes())
    assert _assert_exact_ordering(log) == 3


def test_strict_ordering(live, services):  # noqa: F811
    services.chat = TwoSentenceChat()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        log = _collect(ws, np.zeros(8000, "<i2").tobytes())
    assert _assert_exact_ordering(log) == 2
    types = [item["type"] for kind, item in log if kind == "json"]
    assert types[-1] == "done"
    assert types.index("tts_end") < len(types) - 1


def test_tokens_not_blocked_by_tts(live, services):  # noqa: F811
    services.chat = ThreeSentenceChat()
    services.tts = FixedTts(1600, delay=0.3)
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        log = _collect(ws, np.zeros(8000, "<i2").tobytes())
    events = [item["type"] for kind, item in log if kind == "json"]
    ends = [i for i, t in enumerate(events) if t == "tts_end"]
    tokens = [i for i, t in enumerate(events) if t == "token"]
    assert len(tokens) == 3 and len(ends) == 3
    assert max(tokens) < ends[1]


class RaisingTts:
    async def synthesize(self, text, voice=None) -> bytes:
        raise RuntimeError("piper exploded")


def test_tts_failure_emits_error_not_done(live, services):  # noqa: F811
    services.chat = ThreeSentenceChat()
    services.tts = RaisingTts()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        ws.send(json.dumps({"type": "start", "protocol": 2}))
        ws.send(np.zeros(8000, "<i2").tobytes())
        ws.send(json.dumps({"type": "stop"}))
        events = []
        try:
            while True:
                raw = ws.recv(timeout=2)
                if isinstance(raw, str):
                    events.append(json.loads(raw))
        except TimeoutError:
            pass
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1 and "piper exploded" in errors[0]["error"]["message"]
    assert not any(e["type"] in {"done", "tts_start"} for e in events)


class SlowFirstTokenChat:
    async def stream(self, model, messages, max_tokens, images=None):
        await asyncio.sleep(0.3)
        yield "Done. "


def _handshake(ws, pcm=None):
    ws.send(json.dumps({"type": "start", "protocol": 2, "model": "qwen3-vl"}))
    ws.send(pcm if pcm is not None else np.zeros(8000, "<i2").tobytes())
    ws.send(json.dumps({"type": "stop"}))


def test_transcript_and_ttft_do_not_wait_for_the_first_token(live, services):  # noqa: F811
    services.chat = SlowFirstTokenChat()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        _handshake(ws)
        kinds, done = [], None
        deadline = time.time() + 10
        while done is None and time.time() < deadline:
            raw = ws.recv(timeout=10)
            if isinstance(raw, bytes):
                continue
            event = json.loads(raw)
            kinds.append(event["type"])
            if event["type"] == "done":
                done = event
    assert kinds.index("transcript") < kinds.index("token")
    assert done["timings"]["llm_ttft_ms"] >= 250
    assert done["timings"]["llm_ms"] >= done["timings"]["llm_ttft_ms"]


def test_cancel_while_waiting_for_the_first_token_is_prompt(live, services):  # noqa: F811
    services.chat = SlowFirstTokenChat()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        _handshake(ws)
        while json.loads(ws.recv(timeout=5))["type"] != "transcript":
            pass
        sent = time.perf_counter()
        ws.send(json.dumps({"type": "cancel"}))
        while json.loads(ws.recv(timeout=5))["type"] != "cancelled":
            pass
        assert time.perf_counter() - sent < 0.15
        with pytest.raises(TimeoutError):
            ws.recv(timeout=0.5)


class ChunkedTts:
    async def synthesize(self, text, voice=None):
        raise AssertionError("native streaming must not call whole-sentence synthesis")

    async def synthesize_stream(self, text, voice=None):
        for samples in (1201, 800):
            await asyncio.sleep(0.05)
            yield np.full(samples, 1000, dtype="<i2").tobytes(), 24000


def test_native_tts_streams_chunks_with_final_byte_total(live, services):  # noqa: F811
    services.chat = TwoSentenceChat()
    services.tts = ChunkedTts()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        log = _collect(ws, np.zeros(8000, "<i2").tobytes())
    starts = [item for kind, item in log if kind == "json" and item["type"] == "tts_start"]
    ends = [item for kind, item in log if kind == "json" and item["type"] == "tts_end"]
    assert [event["bytes"] for event in starts] == [0, 0]
    assert len(ends) == 2
    for start, end in zip(starts, ends, strict=True):
        start_index = log.index(("json", start))
        end_index = log.index(("json", end))
        audio = [item for kind, item in log[start_index:end_index] if kind == "bin"]
        assert len(audio) >= 2
        assert sum(map(len, audio)) == end["bytes"]
        assert end["bytes"] % 1280 == 0
    assert log[-1][1]["type"] == "done"


def test_native_tts_does_not_block_llm_tokens(live, services):  # noqa: F811
    services.chat = ThreeSentenceChat()
    services.tts = ChunkedTts()
    with connect(f"ws://127.0.0.1:{live}/v1/voice/stream") as ws:
        log = _collect(ws, np.zeros(8000, "<i2").tobytes())
    events = [item["type"] for kind, item in log if kind == "json"]
    assert max(i for i, kind in enumerate(events) if kind == "token") < events.index("done")
    assert events.index("token") < events.index("tts_end")


async def test_ws_output_leaves_playback_pacing_to_clients():
    from aibox.voice.pipeline import ClientPacedWebsocketOutputTransport

    transport = object.__new__(ClientPacedWebsocketOutputTransport)
    transport._next_send_time = time.monotonic() + 60
    transport._send_interval = 0.04
    started = time.perf_counter()
    await transport._write_audio_sleep()
    assert time.perf_counter() - started < 0.05
