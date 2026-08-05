from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from hailo_platform import VDevice
from hailo_platform.genai import Speech2Text, Speech2TextTask

from nova_hailo.config import ROOT

try:
    from hailo_apps.python.core.common.hailo_logger import get_logger

    logger = get_logger(__name__)
except Exception:
    import logging

    logger = logging.getLogger(__name__)

WHISPER_SMALL = [
    ROOT / "models" / "Whisper-Small.hef",
    Path("/usr/local/hailo/resources/models/hailo10h/Whisper-Small.hef"),
    Path.home() / "Downloads" / "Whisper-Small.hef",
]
WHISPER_BASE = [
    # Prefer HailoRT-bundled HEF (Downloads copy is a different/incompatible blob).
    Path("/usr/local/hailo/resources/models/hailo10h/Whisper-Base.hef"),
    ROOT / "models" / "Whisper-Base.hef",
]

# Classic Whisper garbage on silence / noise / EOS
_HALLUCINATIONS = {
    "thank you for watching",
    "thanks for watching",
    "thanks for listening",
    "please subscribe",
    "subscribe",
    "the end",
    "the end.",
    "bye",
    "bye.",
    "you",
    "thank you",
    "thanks",
    "music",
    "applause",
    "silence",
    "mbc 뉴스",
    "mbc news",
    "www.google.com",
    "subtitles by",
    "amara.org",
    ".",
    "...",
    "the next day, the day of the day, the day of the day. i'm gonna like it.",
}


def _existing(paths) -> list[str]:
    out = []
    for p in paths:
        if p.exists() and p.stat().st_size > 50_000_000:
            out.append(str(p))
    return out


def resolve_whisper_hef(hef_path: str | None = None) -> str:
    if hef_path:
        key = hef_path.strip().lower()
        if key in {"small", "whisper-small", "whisper_small"}:
            paths = _existing(WHISPER_SMALL) or _existing(WHISPER_BASE)
            if paths:
                return paths[0]
        if key in {"base", "whisper-base", "whisper_base"}:
            paths = _existing(WHISPER_BASE)
            if paths:
                return paths[0]
        p = Path(hef_path).expanduser()
        if p.exists():
            return str(p.resolve())
    for p in _existing(WHISPER_BASE) + _existing(WHISPER_SMALL):
        return p
    raise FileNotFoundError("No Whisper HEF found (Small or Base)")


def _is_hallucination(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    if t in _HALLUCINATIONS:
        return True
    # repetitive loops ("the day of the day…", "really really…")
    words = re.findall(r"[a-z']+", t)
    if len(words) >= 4 and len(set(words)) == 1:
        return True
    if len(words) >= 6:
        uniq = len(set(words))
        if uniq <= max(2, len(words) // 4):
            return True
    if "subscribe" in t or "thanks for watching" in t or "thank you for watching" in t:
        return True
    if "for watching" in t or "please like" in t:
        return True
    if t in {"the end.", "the end"}:
        return True
    return False


_KNOWN_WORDS = {
    "a", "an", "and", "are", "at", "back", "be", "by", "calendar", "can", "check",
    "do", "drive", "email", "file", "files", "find", "for", "from", "go", "hello",
    "help", "hey", "hi", "how", "i", "in", "is", "it", "me", "meeting", "meetings",
    "my", "no", "nova", "of", "off", "ok", "okay", "on", "open", "please", "read",
    "schedule", "search", "show", "stop", "tell", "thanks", "that", "the", "this",
    "time", "to", "today", "tomorrow", "up", "was", "we", "web", "what", "when",
    "who", "why", "with", "yes", "you", "your",
}


_STANDALONE_WORDS = {
    "yes", "no", "stop", "cancel", "hello", "hi", "hey", "thanks", "nova",
    "okay", "ok", "goodbye", "bye", "help", "repeat", "again", "continue",
}


def _is_gibberish(text: str) -> bool:
    """Reject transcripts too mangled to be worth sending to a 1.5B model.

    Whisper-Base on short/noisy segments emits things like "Finutikimig calendar"
    or "Zlat." Feeding those to the LLM produces confident nonsense, so treat them
    as no-speech and let the caller ask for a repeat instead.
    """
    t = (text or "").strip()
    if not t:
        return True
    words = re.findall(r"[A-Za-z']+", t.lower())
    if not words:
        return True
    # A lone word is only a real turn if it is a standalone command/answer.
    # Everything else at length 1 is a VAD fragment ("The", "Zlat.", "It").
    if len(words) == 1 and words[0] not in _STANDALONE_WORDS:
        return True
    # Long words with no vowels, or an implausible consonant run, signal garbage.
    for w in words:
        if len(w) >= 6 and not re.search(r"[aeiou]", w):
            return True
        if re.search(r"[bcdfghjklmnpqrstvwxz]{5,}", w):
            return True
    return False


def _normalize_audio(audio: np.ndarray) -> np.ndarray:
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    if len(x) == 0:
        return x
    # Speech2Text expects float32 mono roughly in [-1, 1].
    # Do NOT boost quiet peaks — that turns room noise into Whisper hallucinations.
    peak = float(np.max(np.abs(x)) + 1e-9)
    if peak > 1.0:
        x = x / peak
    return x.astype(np.float32, copy=False)


class WhisperSTT:
    def __init__(self, vdevice: VDevice, hef_path: str | None = None):
        primary = resolve_whisper_hef(hef_path)
        candidates = [primary]
        for b in _existing(WHISPER_BASE):
            if b not in candidates:
                candidates.append(b)

        last_err = None
        self._s2t: Speech2Text | None = None
        self.hef_path = primary
        for path in candidates:
            try:
                print(f"Loading Whisper STT: {path}")
                logger.info("Loading Whisper STT: %s", path)
                self._s2t = Speech2Text(vdevice, path)
                self.hef_path = path
                if path != primary:
                    print(f"  (fell back from {primary})")
                break
            except Exception as e:
                last_err = e
                print(f"  failed: {e}")
                logger.warning("Whisper load failed for %s: %s", path, e)
        if self._s2t is None:
            raise RuntimeError(f"Could not load any Whisper HEF: {last_err}")

    def release(self):
        try:
            if self._s2t is not None and hasattr(self._s2t, "release"):
                self._s2t.release()
        except Exception:
            pass
        self._s2t = None

    def transcribe(self, audio: np.ndarray, language: str = "en") -> str:
        if self._s2t is None:
            raise RuntimeError("Whisper STT already released")

        x = _normalize_audio(audio)
        if len(x) < 1600:  # <0.1s
            return ""

        rms = float(np.sqrt(np.mean(x * x)) + 1e-12)
        # Higher floor: Whisper-Base invents captions on hush / HVAC
        if rms < 0.015:
            print(f"[asr] skip: too quiet (rms={rms:.5f})")
            return ""

        print(f"[asr] audio sec={len(x)/16000:.2f} rms={rms:.4f}")
        segments = self._s2t.generate_all_segments(
            audio_data=x,
            task=Speech2TextTask.TRANSCRIBE,
            language=language,
            timeout_ms=30000,
        )
        if not segments:
            return ""

        full = "".join(seg.text for seg in segments).strip()
        # Whisper emits language/task control tokens (e.g. "<|km|><|en|>") when it
        # is unsure of the language; they reached the UI and the LLM prompt
        # verbatim — measured 2026-07-29. Strip before anything downstream.
        full = re.sub(r"<\|[^|>]{0,24}\|>", " ", full)
        full = re.sub(r"\s+", " ", full).strip()
        if _is_hallucination(full):
            print(f"[asr] dropped hallucination: {full!r}")
            return ""
        if _is_gibberish(full):
            print(f"[asr] dropped gibberish: {full!r}")
            return ""
        return full
