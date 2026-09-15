"""The ack PCM cache must be a pure speed-up: never a new failure mode."""
from __future__ import annotations

import numpy as np
import pytest

from nova_hailo.backends.tts_cache import PcmCache, phrase_key
from nova_hailo.edge_harness.tool_ack import all_ack_lines, choose_tool_ack


class FakeTTS:
    """Counts synthesis calls so we can prove the cache actually avoids them."""

    def __init__(self, fail_on: set[str] | None = None):
        self.calls: list[str] = []
        self.fail_on = fail_on or set()

    def synthesize(self, text: str):
        self.calls.append(text)
        if text in self.fail_on:
            raise RuntimeError("synth exploded")
        return np.ones(120, dtype=np.float32), 22050


def test_every_ack_line_is_pre_renderable() -> None:
    """The warm set must cover what choose_tool_ack can actually return.

    If these drift apart, a line gets picked at the worst possible moment and
    silently costs a live synthesis -- the exact cost the cache exists to
    remove.
    """
    lines = all_ack_lines()
    assert len(lines) == len(set(lines)), "warm set must be de-duplicated"
    for tool in ("web_search", "check_calendar", "deep_research", None, "unknown_tool"):
        for _ in range(30):
            assert choose_tool_ack(tool) in lines


def test_build_then_get_avoids_a_second_synthesis(tmp_path) -> None:
    tts = FakeTTS()
    cache = PcmCache(cache_dir=tmp_path, voice="amy")
    built = cache.build(tts, ["One moment.", "Let me check."])

    assert built == 2
    assert len(tts.calls) == 2
    audio, sample_rate = cache.get("One moment.")
    assert sample_rate == 22050 and len(audio) == 120

    # Building again must not re-synthesise anything already held.
    assert cache.build(tts, ["One moment.", "Let me check."]) == 0
    assert len(tts.calls) == 2


def test_cache_survives_a_restart(tmp_path) -> None:
    """A fresh process loads from disk instead of paying synthesis again."""
    PcmCache(cache_dir=tmp_path, voice="amy").build(FakeTTS(), ["One moment."])

    tts = FakeTTS()
    reopened = PcmCache(cache_dir=tmp_path, voice="amy")
    assert reopened.load(["One moment."]) == 1
    assert reopened.get("One moment.") is not None
    assert tts.calls == []


def test_a_different_voice_does_not_replay_stale_audio(tmp_path) -> None:
    PcmCache(cache_dir=tmp_path, voice="amy").build(FakeTTS(), ["One moment."])

    other = PcmCache(cache_dir=tmp_path, voice="ryan")
    assert other.load(["One moment."]) == 0
    assert other.get("One moment.") is None
    assert phrase_key("One moment.", "amy") != phrase_key("One moment.", "ryan")


def test_synthesis_failure_degrades_to_live_synthesis(tmp_path) -> None:
    """A broken phrase must not take the others down or raise."""
    tts = FakeTTS(fail_on={"Let me check."})
    cache = PcmCache(cache_dir=tmp_path, voice="amy")

    assert cache.build(tts, ["One moment.", "Let me check."]) == 1
    assert cache.get("One moment.") is not None
    # None is the signal to synthesise live, which is today's behaviour.
    assert cache.get("Let me check.") is None


def test_unreadable_cache_file_is_not_fatal(tmp_path) -> None:
    cache = PcmCache(cache_dir=tmp_path, voice="amy")
    cache.build(FakeTTS(), ["One moment."])
    (tmp_path / f"{phrase_key('One moment.', 'amy')}.npz").write_bytes(b"not an npz")

    fresh = PcmCache(cache_dir=tmp_path, voice="amy")
    assert fresh.load(["One moment."]) == 0
    assert fresh.get("One moment.") is None


def test_empty_audio_is_not_cached(tmp_path) -> None:
    """A backend that returns nothing must not poison the cache with silence."""

    class SilentTTS(FakeTTS):
        def synthesize(self, text: str):
            self.calls.append(text)
            return np.zeros(0, dtype=np.float32), 22050

    cache = PcmCache(cache_dir=tmp_path, voice="amy")
    assert cache.build(SilentTTS(), ["One moment."]) == 0
    assert cache.get("One moment.") is None


@pytest.mark.parametrize("missing_dir", ["nested/deeper", "brand_new"])
def test_cache_dir_is_created_on_demand(tmp_path, missing_dir) -> None:
    cache = PcmCache(cache_dir=tmp_path / missing_dir, voice="amy")
    assert cache.build(FakeTTS(), ["One moment."]) == 1
    assert (tmp_path / missing_dir).is_dir()
