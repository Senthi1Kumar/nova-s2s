"""Shared TTS helpers + factory (Piper / Kokoro ONNX on CPU).

Callers: nova_hailo/pipeline.py (~134 create_tts / PiperTTS).
Overwrites existing backends/tts.py (same purpose — extended for Kokoro).
Data files: models/kokoro/kokoro-v1.0.onnx + voices-v1.0.bin (binary weights).
"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from nova_hailo.config import ROOT

try:
    from hailo_apps.python.core.common.hailo_logger import get_logger

    logger = get_logger(__name__)
except Exception:
    import logging

    logger = logging.getLogger(__name__)

DEFAULT_PIPER = ROOT / "models" / "piper" / "en_US-amy-low.onnx"
HAILO_APPS_PIPER = Path("/home/cariad/code/hailo-apps/local_resources/piper_models/en_US-amy-low.onnx")
DEFAULT_KOKORO_ONNX = ROOT / "models" / "kokoro" / "kokoro-v1.0.onnx"
DEFAULT_KOKORO_VOICES = ROOT / "models" / "kokoro" / "voices-v1.0.bin"
KOKORO_ONNX_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
KOKORO_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)


def clean_text_for_tts(text: str) -> str:
    t = text or ""
    if "</think>" in t:
        t = t.split("</think>")[-1]
    if "<think>" in t:
        t = re.sub(r"<think>.*", "", t, flags=re.DOTALL | re.I)
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.DOTALL | re.I)
    # Llama/Qwen special tokens (incl. spaced mangling from bad HEF decode)
    t = re.sub(r"<\|[^|>]{0,60}\|>?", " ", t)
    t = re.sub(r"(?i)(start|end)\s*header\s*id\|?", " ", t)
    t = re.sub(r"<｜[^｜]{0,40}｜>", " ", t)
    for tok in (
        "<|im_end|>",
        "<|eot_id|>",
        "<｜end▁of▁sentence｜>",
        "<|endoftext|>",
        "<|end_of_sentence|>",
        r"\boxed",
    ):
        t = t.replace(tok, "")
    t = re.sub(r"```.*?```", " ", t, flags=re.DOTALL)
    t = re.sub(r"[#*_`>\[\](){}\\]", " ", t)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[|~]{2,}", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    letters = sum(1 for c in t if c.isalpha())
    if letters < 2:
        return ""
    return t


def _find_piper_model(onnx_path: str | None = None) -> Path:
    candidates = []
    if onnx_path:
        candidates.append(Path(onnx_path))
    candidates += [
        DEFAULT_PIPER,
        HAILO_APPS_PIPER,
        Path.home() / ".local/share/piper/en_US-amy-low.onnx",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        f"Piper model not found. Expected {DEFAULT_PIPER} (+ .json). Run model setup first."
    )


def ensure_kokoro_models(
    onnx_path: Path | None = None,
    voices_path: Path | None = None,
) -> tuple[Path, Path]:
    onnx = Path(onnx_path) if onnx_path else DEFAULT_KOKORO_ONNX
    voices = Path(voices_path) if voices_path else DEFAULT_KOKORO_VOICES
    onnx.parent.mkdir(parents=True, exist_ok=True)
    import urllib.request

    if not onnx.exists() or onnx.stat().st_size < 1_000_000:
        print(f"Downloading Kokoro ONNX → {onnx}")
        urllib.request.urlretrieve(KOKORO_ONNX_URL, str(onnx))
    if not voices.exists() or voices.stat().st_size < 1_000_000:
        print(f"Downloading Kokoro voices → {voices}")
        urllib.request.urlretrieve(KOKORO_VOICES_URL, str(voices))
    return onnx, voices


class QueuedOnnxTTS:
    """Non-blocking sentence queue + barge-in; subclasses implement synthesize()."""

    device = "cpu_onnx"

    def __init__(
        self,
        enabled: bool = True,
        device_id: int | None = None,
        wait_for_play: bool = False,
        local_play: bool = True,
        pcm_callback: Callable[[np.ndarray, int], None] | None = None,
    ):
        self.enabled = enabled
        self._device_id = device_id
        self._play_device_ok = True
        self._stream = None
        self._stream_sr = None
        self.output_sample_rate = 16000
        self._wait_for_play = wait_for_play
        self.local_play = local_play
        self.pcm_callback = pcm_callback
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._interrupt = threading.Event()
        self._thread: threading.Thread | None = None
        self._speaking = False
        self._gen_id = 0
        self._gen_lock = threading.Lock()
        self._active_gen = 0
        self.model_path: str | None = None
        self.last_synth_ms: float | None = None
        self.last_play_ms: float | None = None
        self.last_total_ms: float | None = None
        self.first_audio_time: float | None = None
        self.total_synth_ms = 0.0
        self.total_play_ms = 0.0
        self._idle = threading.Event()
        self._idle.set()
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._last_chunk_hit_sentence_end = False
        self._ready = False

    def _start_worker(self):
        self._ready = True
        self._prewarm_playback()
        self._thread = threading.Thread(target=self._worker, daemon=True, name="tts-worker")
        self._thread.start()

    def _prewarm_playback(self) -> None:
        """Resolve the output device at startup, not mid-conversation.

        _play_blocking() walks candidate devices until one opens; doing that on
        the user's first utterances truncated them and printed an ALSA stack per
        failed candidate. Probing 40ms of silence here moves the search (and the
        noise) into startup, so turn 1 plays cleanly — measured 2026-07-29.
        """
        if not self.local_play:
            return
        try:
            import sounddevice as sd

            # Open the stream only — do not write. Writing a silence buffer is
            # what produced the audible tick at startup; opening is silent.
            self._get_stream(sd, self.output_sample_rate)
            logger.info("TTS output prewarmed on device %r", self._device_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TTS playback prewarm failed: %s", exc)

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        raise NotImplementedError

    def synthesize_stream(self, text: str):
        """Yield (audio, sample_rate) per chunk.

        Default: one chunk, so backends that only implement synthesize()
        keep their existing behaviour.
        """
        audio, sample_rate = self.synthesize(text)
        if audio is not None and len(audio) > 0:
            yield audio, sample_rate

    @property
    def is_speaking(self) -> bool:
        return self._speaking or (not self._idle.is_set())

    @property
    def speech_queue(self) -> queue.Queue:
        return self._q

    def get_current_gen_id(self) -> int:
        with self._gen_lock:
            return self._gen_id

    def begin_turn(self) -> int:
        with self._gen_lock:
            self._gen_id += 1
            self._active_gen = self._gen_id
            gid = self._gen_id
        self.first_audio_time = None
        self.total_synth_ms = 0.0
        self.total_play_ms = 0.0
        self.clear_interruption()
        return gid

    def interrupt(self):
        self._interrupt.set()
        with self._gen_lock:
            self._active_gen = -1
        st = getattr(self, "_stream", None)
        if st is not None:
            try:
                st.abort()   # drop queued frames now
                st.start()   # keep it open and ready for the next turn
            except Exception:
                self._close_stream()
        try:
            while True:
                self._q.get_nowait()
                with self._pending_lock:
                    self._pending = max(0, self._pending - 1)
        except queue.Empty:
            pass
        self._idle.set()

    def clear_interruption(self):
        self._interrupt.clear()

    def enqueue(self, text: str, gen_id: int | None = None) -> None:
        if not self.enabled or not self._ready:
            return
        text = clean_text_for_tts(text)
        if not text:
            return
        if gen_id is None:
            gen_id = self.get_current_gen_id()
        with self._pending_lock:
            self._pending += 1
            self._idle.clear()
        self._q.put((gen_id, text, None, None))

    def queue_text(self, text: str, gen_id: int | None = None):
        self.enqueue(text, gen_id)

    def chunk_and_queue(self, buffer: str, gen_id: int, is_first_chunk: bool) -> str:
        """Queue only on sentence ends (.?!) — never mid-phrase word splits."""
        delimiters = [".", "?", "!"]
        hit_sentence_end = False
        while True:
            positions = {buffer.find(d): d for d in delimiters if buffer.find(d) != -1}
            if not positions:
                break
            first_pos = min(positions.keys())
            delim = buffer[first_pos]
            chunk = buffer[: first_pos + 1]
            if chunk.strip():
                self.queue_text(chunk.strip(), gen_id)
                if delim in ".?!":
                    hit_sentence_end = True
            buffer = buffer[first_pos + 1 :]
        self._last_chunk_hit_sentence_end = hit_sentence_end
        return buffer

    def speak(self, text: str, wait: bool | None = None) -> dict:
        empty = {
            "tts_synth_ms": None,
            "tts_play_ms": None,
            "tts_ms": None,
            "device": self.device,
            "ttfa_wall": self.first_audio_time,
        }
        if not self.enabled or not self._ready or not text:
            return empty
        text = clean_text_for_tts(text)
        if not text:
            return empty
        self.clear_interruption()
        gid = self.begin_turn()
        done = threading.Event()
        result: dict = {}
        with self._pending_lock:
            self._pending += 1
            self._idle.clear()
        self._q.put((gid, text, done, result))
        do_wait = self._wait_for_play if wait is None else wait
        if do_wait:
            done.wait(timeout=60)
            self.last_synth_ms = result.get("tts_synth_ms")
            self.last_play_ms = result.get("tts_play_ms")
            self.last_total_ms = result.get("tts_ms")
            return {
                "tts_synth_ms": self.last_synth_ms,
                "tts_play_ms": self.last_play_ms,
                "tts_ms": self.last_total_ms,
                "device": self.device,
                "ttfa_wall": self.first_audio_time,
            }
        return empty

    def wait_drain(self, timeout: float = 60.0) -> bool:
        return self._idle.wait(timeout=timeout)

    def timing_snapshot(self) -> dict:
        return {
            "tts_synth_ms": round(self.total_synth_ms, 1) if self.total_synth_ms else None,
            "tts_play_ms": round(self.total_play_ms, 1) if self.total_play_ms else None,
            "tts_ms": round(self.total_synth_ms + self.total_play_ms, 1)
            if (self.total_synth_ms or self.total_play_ms)
            else None,
            "device": self.device,
            "ttfa_wall": self.first_audio_time,
        }

    def stop(self):
        self._stop.set()
        self.interrupt()
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout=2)
        self._close_stream()

    def _get_stream(self, sd, sample_rate: int):
        """One persistent OutputStream for the process, opened lazily.

        ``sd.play()`` opens and tears down an ALSA stream per call. With clause-
        level streaming TTS that is one device open per clause, which on the
        WM8960's dmix path prints an ALSA stack, clicks, and truncates the first
        clauses while the device settles. Holding one stream open and writing into
        it removes all three.
        """
        st = getattr(self, "_stream", None)
        if st is not None and getattr(self, "_stream_sr", None) == sample_rate:
            return st
        self._close_stream()
        last = None
        # Playback-only PCMs first, and never hold "default"/None: on the WM8960
        # asound.conf "default" is asym{playback,capture}, so keeping it open for
        # output locks the capture side and the microphone goes dead everywhere
        # (browser and CLI) — measured 2026-07-30. "playback" is plug->dmix only.
        # On a PipeWire-managed host (WirePlumber owns hw:2) a raw ALSA hold on
        # the WM8960 blocks PipeWire's capture of the same codec, killing the
        # browser mic. Route through the pulse bridge first: PipeWire is designed
        # to share the card. Raw dmix only if PipeWire is absent.
        candidates: list = ["pulse", "playback", "dmixed"]
        if self._device_id not in candidates:
            candidates.append(self._device_id)
        candidates.append(None)  # last resort only
        for dev in candidates:
            try:
                st = sd.OutputStream(
                    samplerate=sample_rate,
                    channels=1,
                    dtype="float32",
                    device=dev,
                    latency="high",
                )
                st.start()
                self._stream = st
                self._stream_sr = sample_rate
                self._device_id = dev
                logger.info("TTS output stream open on %r @ %dHz", dev, sample_rate)
                return st
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise last if last is not None else RuntimeError("no usable output device")

    def _close_stream(self) -> None:
        st = getattr(self, "_stream", None)
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass
        self._stream = None
        self._stream_sr = None

    def _play_blocking(self, sd, audio, sample_rate: int) -> None:
        """Write one clause into the persistent stream (blocks until consumed)."""
        import numpy as _np

        st = self._get_stream(sd, int(sample_rate))
        buf = _np.ascontiguousarray(_np.asarray(audio, dtype=_np.float32).reshape(-1, 1))
        # Chunked writes so barge-in can abandon the rest of a long clause.
        step = max(1024, int(sample_rate * 0.05))
        for i in range(0, len(buf), step):
            if self._interrupt.is_set():
                return
            st.write(buf[i : i + step])

    def _play_blocking_open_close(self, sd, audio, sample_rate: int) -> None:
        """Play one clause, self-healing past a bad device index.

        PortAudio device indices are not stable across processes: the index that
        resolves to the ALSA "playback" plug PCM in one process can point at a
        raw hw device in another, which collides with dmix and raises
        -9985 Device unavailable (measured 2026-07-29). On the first such
        failure, fall back to the ALSA default — asound.conf already routes it
        through plug->dmix — and remember that choice for the session.
        """
        candidates = [self._device_id] if self._play_device_ok else []
        candidates += [d for d in ("playback", "default", None) if d not in candidates]
        last_exc = None
        for dev in candidates:
            try:
                sd.play(audio, samplerate=sample_rate, device=dev)
                sd.wait()
                if dev != self._device_id:
                    logger.warning("TTS playback fell back to device %r", dev)
                    self._device_id = dev
                self._play_device_ok = True
                return
            except Exception as exc:  # noqa: BLE001 - PortAudioError varies
                last_exc = exc
                self._play_device_ok = False
        if last_exc is not None:
            raise last_exc

    def _worker(self):
        import sounddevice as sd

        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                break
            if len(item) == 4:
                gen_id, text, done_ev, result = item
            else:
                gen_id, text = item[0], item[1]
                done_ev, result = None, {}

            if self._interrupt.is_set() or gen_id != self._active_gen:
                if done_ev:
                    done_ev.set()
                with self._pending_lock:
                    self._pending = max(0, self._pending - 1)
                    if self._pending == 0:
                        self._idle.set()
                continue
            try:
                self._speaking = True
                t0 = time.perf_counter()
                synth_ms = 0.0
                play_ms = 0.0
                emitted = False
                for audio, sample_rate in self.synthesize_stream(text):
                    if self._interrupt.is_set() or gen_id != self._active_gen:
                        break
                    if not emitted:
                        synth_ms = (time.perf_counter() - t0) * 1000.0
                        emitted = True
                    t1 = time.perf_counter()
                    if self.first_audio_time is None:
                        self.first_audio_time = t1
                    if self.pcm_callback is not None:
                        try:
                            self.pcm_callback(audio, int(sample_rate))
                        except Exception as e:
                            logger.error("TTS pcm_callback error: %s", e)
                    if self.local_play:
                        self._play_blocking(sd, audio, sample_rate)
                    play_ms += max(0.0, (time.perf_counter() - t1) * 1000.0)
                if not emitted:
                    synth_ms = (time.perf_counter() - t0) * 1000.0
                self.total_synth_ms += synth_ms
                self.total_play_ms += play_ms
                if result is not None:
                    result["tts_synth_ms"] = round(synth_ms, 1)
                    result["tts_play_ms"] = round(play_ms, 1)
                    result["tts_ms"] = round(synth_ms + play_ms, 1)
                self.last_synth_ms = round(synth_ms, 1)
                self.last_play_ms = round(play_ms, 1)
                self.last_total_ms = round(synth_ms + play_ms, 1)
            except Exception as e:
                logger.error("TTS playback error: %s", e)
                print(f"[tts] error: {e}")
            finally:
                self._speaking = False
                if done_ev:
                    done_ev.set()
                with self._pending_lock:
                    self._pending = max(0, self._pending - 1)
                    if self._pending == 0:
                        self._idle.set()


class PiperTTS(QueuedOnnxTTS):
    device = "cpu_onnx_piper"

    def __init__(
        self,
        enabled: bool = True,
        model_path: str | None = None,
        device_id: int | None = None,
        wait_for_play: bool = False,
        local_play: bool = True,
        pcm_callback: Callable[[np.ndarray, int], None] | None = None,
    ):
        super().__init__(
            enabled=enabled,
            device_id=device_id,
            wait_for_play=wait_for_play,
            local_play=local_play,
            pcm_callback=pcm_callback,
        )
        self._voice = None
        if not enabled:
            return
        try:
            from piper import PiperVoice

            onnx = _find_piper_model(model_path)
            self.model_path = str(onnx)
            logger.info("Loading Piper TTS: %s", onnx)
            print(f"Loading Piper TTS: {onnx}")
            self._voice = PiperVoice.load(str(onnx))
            self._start_worker()
        except Exception as e:
            logger.warning("TTS disabled: %s", e)
            print(f"TTS disabled: {e}")
            self.enabled = False

    @staticmethod
    def _chunk_to_float32(audio_chunk, sample_rate: int):
        if hasattr(audio_chunk, "audio_float_array"):
            arr = np.asarray(audio_chunk.audio_float_array, dtype=np.float32)
            sample_rate = getattr(audio_chunk, "sample_rate", sample_rate) or sample_rate
        elif hasattr(audio_chunk, "audio_int16_bytes"):
            arr = (
                np.frombuffer(audio_chunk.audio_int16_bytes, dtype=np.int16).astype(np.float32)
                / 32768.0
            )
            sample_rate = getattr(audio_chunk, "sample_rate", sample_rate) or sample_rate
        else:
            arr = np.asarray(audio_chunk, dtype=np.float32)
        if arr.ndim > 1:
            arr = arr.reshape(-1)
        return arr, sample_rate

    def synthesize_stream(self, text: str):
        sample_rate = 22050
        for audio_chunk in self._voice.synthesize(text):
            if self._interrupt.is_set():
                break
            arr, sample_rate = self._chunk_to_float32(audio_chunk, sample_rate)
            if arr.size:
                yield arr, sample_rate

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        chunks = []
        sample_rate = 22050
        for arr, sample_rate in self.synthesize_stream(text):
            chunks.append(arr)
        if not chunks:
            return np.zeros(0, dtype=np.float32), sample_rate
        return np.concatenate(chunks), sample_rate


class KokoroTTS(QueuedOnnxTTS):
    """Kokoro v1 ONNX via kokoro-onnx (onnxruntime, like Piper)."""

    device = "cpu_onnx_kokoro"

    def __init__(
        self,
        enabled: bool = True,
        model_path: str | None = None,
        voices_path: str | None = None,
        voice: str = "af_sarah",
        speed: float = 1.0,
        device_id: int | None = None,
        wait_for_play: bool = False,
        local_play: bool = True,
        pcm_callback: Callable[[np.ndarray, int], None] | None = None,
    ):
        super().__init__(
            enabled=enabled,
            device_id=device_id,
            wait_for_play=wait_for_play,
            local_play=local_play,
            pcm_callback=pcm_callback,
        )
        self._kokoro = None
        self.voice = voice
        self.speed = speed
        if not enabled:
            return
        try:
            from kokoro_onnx import Kokoro

            onnx, voices = ensure_kokoro_models(
                Path(model_path) if model_path else None,
                Path(voices_path) if voices_path else None,
            )
            self.model_path = str(onnx)
            print(f"Loading Kokoro TTS: {onnx} voice={voice}")
            logger.info("Loading Kokoro TTS: %s voice=%s", onnx, voice)
            self._kokoro = Kokoro(str(onnx), str(voices))
            self._start_worker()
        except Exception as e:
            logger.warning("Kokoro TTS disabled: %s", e)
            print(f"Kokoro TTS disabled: {e}")
            self.enabled = False

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        text = clean_text_for_tts(text)
        if not text:
            return np.zeros(0, dtype=np.float32), 24000
        try:
            samples, sample_rate = self._kokoro.create(
                text, voice=self.voice, speed=self.speed, lang="en-us"
            )
        except Exception as e:
            # Phonemizer/index bugs on junk text — skip rather than crash the turn
            logger.error("Kokoro synthesize failed: %s", e)
            print(f"[tts] kokoro skip: {e}")
            return np.zeros(0, dtype=np.float32), 24000
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        return audio, int(sample_rate)


def create_tts(
    engine: str | None = None,
    *,
    enabled: bool = True,
    piper_path: str | None = None,
    kokoro_onnx: str | None = None,
    kokoro_voices: str | None = None,
    kokoro_voice: str = "af_sarah",
    inflect_model_dir: str | None = None,
    inflect_speed: float = 1.0,
    inflect_variation: float = 0.667,
    inflect_seed: int = 7,
    device_id: int | None = None,
    wait_for_play: bool = False,
    local_play: bool = True,
    pcm_callback: Callable[[np.ndarray, int], None] | None = None,
) -> QueuedOnnxTTS:
    """Build Piper, Kokoro or MOSS TTS. Env NOVA_HAILO_TTS overrides config."""
    eng = (engine or os.environ.get("NOVA_HAILO_TTS") or "kokoro").strip().lower()
    if eng in {"inflect", "inflect-nano", "inflect_nano", "inflect-nano-v2"}:
        from nova_hailo.backends.inflect_tts import InflectTTS

        tts = InflectTTS(
            enabled=enabled,
            model_dir=inflect_model_dir,
            speed=inflect_speed,
            variation=inflect_variation,
            seed=inflect_seed,
            device_id=device_id,
            wait_for_play=wait_for_play,
            local_play=local_play,
            pcm_callback=pcm_callback,
        )
        if tts.enabled:
            return tts
        print("Falling back to Piper TTS")
        eng = "piper"
    if eng in {"kokoro", "kokoro-onnx", "k"}:
        tts = KokoroTTS(
            enabled=enabled,
            model_path=kokoro_onnx,
            voices_path=kokoro_voices,
            voice=os.environ.get("NOVA_HAILO_KOKORO_VOICE") or kokoro_voice,
            device_id=device_id,
            wait_for_play=wait_for_play,
            local_play=local_play,
            pcm_callback=pcm_callback,
        )
        if tts.enabled:
            return tts
        print("Falling back to Piper TTS")
        eng = "piper"
    return PiperTTS(
        enabled=enabled,
        model_path=piper_path,
        device_id=device_id,
        wait_for_play=wait_for_play,
        local_play=local_play,
        pcm_callback=pcm_callback,
    )
