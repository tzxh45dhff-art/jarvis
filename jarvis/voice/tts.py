"""
Text to speech via the local Kokoro-82M MLX model already on this machine.

Speech is generated per sentence and queued for playback, so the first words are
audible while the rest is still being synthesised. Warm real-time factor is
about 0.04, i.e. ~25x faster than playback.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from typing import Iterator, Protocol

import numpy as np

from ..config import CONFIG

log = logging.getLogger("jarvis.tts")
_PATCHED = False


def _patch_kokoro() -> None:
    """
    mlx-audio 0.4.4 has a length bug in Kokoro's harmonic source generator: the
    sine waveform comes back a few frames longer than the F0 contour it must be
    multiplied by, so synthesis raises a broadcast error on many inputs.

    Patched at runtime rather than in site-packages so the user's shared
    environment is left untouched.
    """
    global _PATCHED
    if _PATCHED:
        return
    _init_espeak()
    try:
        import mlx.core as mx
        from mlx_audio.tts.models.kokoro import istftnet as I
    except Exception as e:                       # pragma: no cover
        log.warning("kokoro patch skipped: %s", e)
        return

    def patched_call(self, f0):
        n = f0.shape[1]
        fn = f0 * mx.arange(1, self.harmonic_num + 2)[None, None, :]
        sine = self._f02sine(fn) * self.sine_amp
        t = sine.shape[1]
        if t != n:
            if t > n:
                sine = sine[:, :n, :]
            else:
                sine = mx.concatenate(
                    [sine, mx.repeat(sine[:, -1:, :], n - t, axis=1)], axis=1)
        uv = self._f02uv(f0)
        noise_amp = uv * self.noise_std + (1 - uv) * self.sine_amp / 3
        noise = noise_amp * mx.random.normal(sine.shape)
        return sine * uv + noise, uv, noise

    I.SineGen.__call__ = patched_call
    _PATCHED = True
    log.info("applied Kokoro SineGen length fix")


def _init_espeak() -> bool:
    """
    Point phonemizer at the bundled espeak-ng shared library.

    Without this, Kokoro's grapheme-to-phoneme stage logs "EspeakFallback not
    Enabled: OOD words will be skipped" and silently drops any word missing from
    its dictionary - names, jargon, product names. The library ships inside the
    `espeakng-loader` wheel, so nothing needs installing; it just has to be
    registered before misaki builds its pipeline.
    """
    try:
        import os
        import espeakng_loader
        lib = espeakng_loader.get_library_path()
        data = espeakng_loader.get_data_path()
        os.environ.setdefault("ESPEAK_DATA_PATH", str(data))
        os.environ.setdefault("PHONEMIZER_ESPEAK_LIBRARY", str(lib))
        try:
            from phonemizer.backend.espeak.wrapper import EspeakWrapper
            EspeakWrapper.set_library(str(lib))
            if hasattr(EspeakWrapper, "set_data_path"):
                EspeakWrapper.set_data_path(str(data))
        except Exception:
            pass
        return True
    except Exception as e:
        log.info("espeak fallback unavailable (%s); rare words may be skipped", e)
        return False


_SENT_END = re.compile(r"(?<=[.!?])\s+|(?<=[.!?])$|\n{2,}")


def split_sentences(text: str, min_len: int = 24, first_min: int = 10) -> list[str]:
    """
    Chunk text for streaming synthesis without cutting mid-thought.

    The first chunk is allowed to be short so speech starts as soon as possible;
    later chunks are longer, which sounds more natural and synthesises faster
    per second of audio.
    """
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    parts: list[str] = []
    cur = ""
    for piece in _SENT_END.split(text):
        if not piece:
            continue
        cur = (cur + " " + piece).strip() if cur else piece
        limit = first_min if not parts else min_len
        if len(cur) >= limit:
            parts.append(cur)
            cur = ""
    if cur:
        if parts and len(cur) < 12:
            parts[-1] += " " + cur
        else:
            parts.append(cur)
    return parts


def clean_for_speech(text: str) -> str:
    """Strip markdown and other things that sound wrong read aloud."""
    t = text
    t = re.sub(r"```.*?```", " (code omitted) ", t, flags=re.DOTALL)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", " a link ", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", t)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\d+[.)]\s+", "", t, flags=re.M)
    t = re.sub(r"[|_>#]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


class TTSBackend(Protocol):
    def synthesize(self, text: str) -> np.ndarray: ...
    @property
    def sample_rate(self) -> int: ...


class KokoroTTS:
    def __init__(self, model_path: str | None = None, voice: str | None = None):
        self.model_path = model_path or CONFIG.voice.tts_model
        self.voice = voice or CONFIG.voice.tts_voice
        self._model = None
        self._lock = threading.Lock()

    @property
    def sample_rate(self) -> int:
        return CONFIG.voice.tts_sample_rate

    def load(self):
        with self._lock:
            if self._model is None:
                _patch_kokoro()
                from mlx_audio.tts.utils import load_model
                t0 = time.time()
                self._model = load_model(self.model_path)
                log.info("kokoro loaded in %.2fs", time.time() - t0)
            return self._model

    def synthesize(self, text: str, voice: str | None = None,
                   speed: float | None = None) -> np.ndarray:
        text = (text or "").strip()
        if not text:
            return np.zeros(0, dtype=np.float32)
        m = self.load()
        chunks = []
        with self._lock:
            for seg in m.generate(text=text, voice=voice or self.voice,
                                  speed=speed if speed is not None else CONFIG.voice.tts_speed,
                                  lang_code=CONFIG.voice.tts_lang):
                a = seg.audio if hasattr(seg, "audio") else seg
                chunks.append(np.asarray(a, dtype=np.float32).reshape(-1))
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def warm(self) -> None:
        try:
            self.synthesize("Ready.")
        except Exception as e:
            log.warning("tts warmup failed: %s", e)


class Speaker:
    """
    Playback with a synthesis worker in front of it.

    `say_stream` accepts text as it arrives from the model, so speech starts
    after the first sentence rather than after the full answer.
    """

    def __init__(self, backend: TTSBackend | None = None):
        self.tts = backend or KokoroTTS()
        self._audio_q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._playing = threading.Event()
        self._player: threading.Thread | None = None
        self.muted = False

    # -- playback ----------------------------------------------------------
    def _ensure_player(self) -> None:
        if self._player and self._player.is_alive():
            return
        self._player = threading.Thread(target=self._play_loop, daemon=True)
        self._player.start()

    def _play_loop(self) -> None:
        import sounddevice as sd
        sr = self.tts.sample_rate
        while not self._stop.is_set():
            try:
                audio = self._audio_q.get(timeout=0.2)
            except queue.Empty:
                self._playing.clear()
                continue
            if audio is None:
                self._playing.clear()
                continue
            try:
                self._playing.set()
                sd.play(audio, sr, blocking=False)
                # poll so an interrupt can cut playback mid-utterance
                dur = len(audio) / sr
                t0 = time.time()
                while time.time() - t0 < dur + 0.05:
                    if self._stop.is_set():
                        sd.stop()
                        break
                    time.sleep(0.02)
            except Exception as e:
                log.warning("playback failed: %s", e)
            finally:
                if self._audio_q.empty():
                    self._playing.clear()

    def say(self, text: str, block: bool = False) -> None:
        if self.muted:
            return
        spoken = clean_for_speech(text)
        if not spoken:
            return
        self._stop.clear()
        self._ensure_player()
        for chunk in split_sentences(spoken):
            if self._stop.is_set():
                break
            try:
                audio = self.tts.synthesize(chunk)
            except Exception as e:
                log.warning("synthesis failed for %r: %s", chunk[:40], e)
                continue
            if len(audio):
                self._audio_q.put(audio)
        if block:
            self.wait()

    def say_stream(self, chunks: Iterator[str]) -> str:
        """Speak while the text is still streaming in. Returns the full text."""
        buf, full = "", []
        self._stop.clear()
        self._ensure_player()
        for c in chunks:
            full.append(c)
            buf += c
            sents = split_sentences(clean_for_speech(buf))
            while len(sents) > 1:
                head, buf = sents[0], " ".join(sents[1:])
                if not self.muted and not self._stop.is_set():
                    try:
                        a = self.tts.synthesize(head)
                        if len(a):
                            self._audio_q.put(a)
                    except Exception as e:
                        log.warning("stream synthesis failed: %s", e)
                sents = split_sentences(buf)
        tail = clean_for_speech(buf)
        if tail and not self.muted and not self._stop.is_set():
            try:
                a = self.tts.synthesize(tail)
                if len(a):
                    self._audio_q.put(a)
            except Exception as e:
                log.warning("tail synthesis failed: %s", e)
        return "".join(full)

    # -- control -----------------------------------------------------------
    def begin(self) -> None:
        """Get ready to receive audio via `enqueue` (clears a prior stop)."""
        self._stop.clear()
        self._ensure_player()

    def enqueue(self, audio: np.ndarray) -> None:
        """Queue already-synthesised audio for playback."""
        if len(audio) and not self.muted and not self._stop.is_set():
            self._audio_q.put(audio)

    def stop(self) -> None:
        """Barge-in: drop queued speech and cut playback immediately."""
        self._stop.set()
        while not self._audio_q.empty():
            try:
                self._audio_q.get_nowait()
            except queue.Empty:
                break
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        self._playing.clear()

    @property
    def speaking(self) -> bool:
        return self._playing.is_set() or not self._audio_q.empty()

    def wait(self, timeout: float = 120.0) -> None:
        t0 = time.time()
        while self.speaking and time.time() - t0 < timeout:
            time.sleep(0.05)

    def warm(self) -> None:
        if hasattr(self.tts, "warm"):
            self.tts.warm()
