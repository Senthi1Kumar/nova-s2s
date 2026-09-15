"""Pre-rendered PCM for the handful of phrases we say over and over.

Acknowledgements from `nova_hailo.edge_harness.tool_ack` are a fixed, tiny set
of strings, but each one costs a full Piper synthesis at the exact moment we
are trying to cover dead air -- 137 ms at p50 and 207 ms at worst on the Pi.
Synthesising them once and replaying the PCM turns that into a 1 us lookup.

This matters more than it used to. On the Pi a cloud turn's first token costs
~576 ms, of which ~350 ms is Groq queue time that no client-side change
reaches, so perceived latency is the only lever left on that path -- and an
acknowledgement that itself takes 137 ms to produce is spending a quarter of
the budget it was meant to save.

The whole bank is 18 phrases: ~2.5 s to build once in the background, 1.7 MB
on disk, 11 ms to reload at startup.

Deliberately generic: it caches any short text under a voice key and knows
nothing about tools or turns. `tool_ack` decides what to say; this decides how
fast it can be said.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CACHE_DIR = ROOT / "runtime" / "filler_cache"


def phrase_key(text: str, voice: str) -> str:
    """Cache key. Voice is part of it so switching TTS never replays stale audio."""
    return hashlib.sha1(f"{voice}\x00{text}".encode("utf-8")).hexdigest()[:16]


class PcmCache:
    """Disk-backed PCM cache for short, fixed phrases.

    Every operation is best-effort. A miss, an unreadable file, or a synthesis
    failure all degrade to "synthesise it live", which is exactly today's
    behaviour -- so a broken cache can make the system slower but never wrong.
    """

    def __init__(self, cache_dir: Path | None = None, voice: str = "default"):
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)
        self.voice = voice
        self._pcm: dict[str, tuple] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._pcm)

    def _path(self, text: str) -> Path:
        return self.cache_dir / f"{phrase_key(text, self.voice)}.npz"

    def get(self, text: str):
        """Cached ``(audio, sample_rate)``, or None to synthesise live."""
        with self._lock:
            return self._pcm.get(text)

    def load(self, texts) -> int:
        """Populate from disk. Returns how many phrases were loaded."""
        import numpy as np

        loaded = 0
        for text in texts:
            path = self._path(text)
            if not path.exists():
                continue
            try:
                with np.load(path) as data:
                    audio = data["audio"]
                    sample_rate = int(data["sample_rate"])
            except Exception as exc:  # noqa: BLE001 - a bad cache file is not fatal
                logger.warning("pcm cache unreadable %s: %s", path.name, exc)
                continue
            with self._lock:
                self._pcm[text] = (audio, sample_rate)
            loaded += 1
        return loaded

    def build(self, tts, texts) -> int:
        """Synthesise and cache anything missing. Returns how many were built."""
        import numpy as np

        built = 0
        for text in texts:
            with self._lock:
                if text in self._pcm:
                    continue
            try:
                audio, sample_rate = tts.synthesize(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("pcm cache synth failed for %r: %s", text, exc)
                continue
            if audio is None or len(audio) == 0:
                continue
            with self._lock:
                self._pcm[text] = (audio, int(sample_rate))
            built += 1
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                np.savez(self._path(text), audio=audio, sample_rate=int(sample_rate))
            except Exception as exc:  # noqa: BLE001 - the memory cache still works
                logger.warning("pcm cache write failed for %r: %s", text, exc)
        return built

    def warm_async(self, tts, texts) -> threading.Thread:
        """Load then build, off the startup path.

        Synthesising the full bank costs a few seconds of CPU; doing it inline
        would delay the first turn to speed up later ones, which is the wrong
        trade on a device that may be demoed thirty seconds after boot.
        """
        texts = list(texts)

        def _run():
            try:
                self.load(texts)
                built = self.build(tts, texts)
                logger.info(
                    "pcm cache ready: %d phrase(s), %d newly synthesised",
                    len(self), built,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("pcm cache warm failed: %s", exc)

        thread = threading.Thread(target=_run, daemon=True, name="pcm-cache-warm")
        thread.start()
        return thread
