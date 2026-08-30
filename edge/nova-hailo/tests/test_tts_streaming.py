"""TTS emits its first chunk before later chunks are synthesized."""
from __future__ import annotations

import numpy as np

from nova_hailo.backends.tts import QueuedOnnxTTS


def test_base_synthesize_stream_wraps_synthesize():
    class _OneShot(QueuedOnnxTTS):
        def synthesize(self, text: str):
            return np.ones(10, dtype=np.float32), 16000

    out = list(_OneShot().synthesize_stream("hello"))
    assert len(out) == 1
    audio, sr = out[0]
    assert sr == 16000 and len(audio) == 10


def test_base_synthesize_stream_skips_empty_audio():
    class _Empty(QueuedOnnxTTS):
        def synthesize(self, text: str):
            return np.zeros(0, dtype=np.float32), 22050

    assert list(_Empty().synthesize_stream("")) == []


def test_stream_yields_chunks_lazily():
    """The first chunk must be available before the second is produced."""
    produced: list[int] = []

    class _TwoChunk(QueuedOnnxTTS):
        def synthesize(self, text: str):
            return np.ones(100, dtype=np.float32), 22050

        def synthesize_stream(self, text: str):
            for i in range(2):
                produced.append(i)
                yield np.full(50, float(i), dtype=np.float32), 22050

    gen = _TwoChunk().synthesize_stream("two sentences here")
    first, _ = next(gen)
    assert produced == [0]  # second chunk not synthesized yet
    assert float(first[0]) == 0.0
    next(gen)
    assert produced == [0, 1]
