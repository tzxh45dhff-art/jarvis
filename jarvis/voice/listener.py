"""
Microphone pipeline: wake word -> voice activity -> utterance capture.

One input stream feeds everything, so the wake detector, the VAD and barge-in
all see the same audio without opening the device more than once.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

import numpy as np

from ..config import CONFIG
from .vad import best_vad
from .wake import WakeWordDetector

log = logging.getLogger("jarvis.listener")

SR = 16000
BLOCK = 1280            # 80 ms


@dataclass
class Utterance:
    audio: np.ndarray
    duration: float
    triggered_by: str = "vad"


class Microphone:
    """Shared, reference-counted input stream."""

    def __init__(self, samplerate: int = SR, block: int = BLOCK):
        self.sr = samplerate
        self.block = block
        self._q: queue.Queue = queue.Queue(maxsize=256)
        self._stream = None
        self._refs = 0
        self._lock = threading.Lock()

    def _cb(self, indata, frames, time_info, status):  # noqa: ARG002
        if status:
            log.debug("audio status: %s", status)
        try:
            self._q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(indata[:, 0].copy())
            except queue.Empty:
                pass

    def start(self) -> None:
        with self._lock:
            self._refs += 1
            if self._stream is not None:
                return
            import sounddevice as sd
            self._stream = sd.InputStream(
                samplerate=self.sr, channels=1, dtype="float32",
                blocksize=self.block, callback=self._cb)
            self._stream.start()
            log.info("microphone open (%d Hz)", self.sr)

    def stop(self) -> None:
        with self._lock:
            self._refs = max(0, self._refs - 1)
            if self._refs == 0 and self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
                log.info("microphone closed")

    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> None:
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.stop()


MIC = Microphone()


def capture_utterance(mic: Microphone | None = None,
                      max_seconds: float | None = None,
                      silence_ms: int | None = None,
                      start_timeout: float = 8.0,
                      preroll: np.ndarray | None = None,
                      on_state=None,
                      stop_event: "threading.Event | None" = None) -> Utterance | None:
    """
    Record until the speaker stops. Returns None if nobody spoke.

    `preroll` lets the caller prepend audio already buffered (e.g. the tail of
    the wake-word frame) so the first syllable is not clipped.
    """
    mic = mic or MIC
    max_seconds = max_seconds or CONFIG.voice.max_utterance_s
    silence_ms = silence_ms or CONFIG.voice.vad_silence_ms
    vad = best_vad(CONFIG.voice.vad_energy_floor)
    if hasattr(vad, "reset"):
        vad.reset()

    frames: list[np.ndarray] = []
    if preroll is not None and len(preroll):
        frames.append(preroll.astype(np.float32))
    started = False
    silent_for = 0.0
    speech_for = 0.0
    t0 = time.time()
    frame_s = BLOCK / SR

    mic.start()
    try:
        while True:
            # Push-to-talk: the key going up ends the utterance immediately,
            # regardless of whether the speaker has paused.
            if stop_event is not None and stop_event.is_set() and started:
                break
            if time.time() - t0 > max_seconds + start_timeout:
                break
            f = mic.read(timeout=1.0)
            if f is None:
                if not started and time.time() - t0 > start_timeout:
                    return None
                continue
            speech = bool(vad.is_speech(f))
            if not started:
                if stop_event is not None and stop_event.is_set():
                    return None
                if time.time() - t0 > start_timeout:
                    return None
                if speech:
                    speech_for += frame_s
                    frames.append(f)
                    if speech_for * 1000 >= CONFIG.voice.vad_start_ms:
                        started = True
                        if on_state:
                            on_state("listening")
                else:
                    speech_for = 0.0
                    # keep a short rolling pre-roll so onsets are not lost
                    frames.append(f)
                    if len(frames) > 8:
                        frames.pop(0)
                continue

            frames.append(f)
            if speech:
                silent_for = 0.0
            else:
                silent_for += frame_s
                if silent_for * 1000 >= silence_ms:
                    break
            if (len(frames) * frame_s) > max_seconds:
                break
    finally:
        mic.stop()

    if not started or not frames:
        return None
    audio = np.concatenate(frames)
    dur = len(audio) / SR
    if dur < 0.3:
        return None
    return Utterance(audio=audio, duration=dur)


def wait_for_wake(mic: Microphone | None = None,
                  detector: WakeWordDetector | None = None,
                  stop_event: threading.Event | None = None,
                  on_tick=None) -> np.ndarray | None:
    """
    Block until the wake word is heard. Returns a short audio pre-roll so the
    command that follows immediately is not clipped.
    """
    mic = mic or MIC
    det = detector or WakeWordDetector()
    if not det.load():
        return None
    mic.start()
    ring: list[np.ndarray] = []
    try:
        while not (stop_event and stop_event.is_set()):
            f = mic.read(timeout=0.5)
            if f is None:
                continue
            ring.append(f)
            if len(ring) > 6:
                ring.pop(0)
            if on_tick:
                on_tick(det.last_score)
            if det.feed(f):
                det.reset()
                return np.concatenate(ring[-2:]) if ring else None
    finally:
        mic.stop()
    return None


def detect_barge_in(mic: Microphone | None = None,
                    stop_event: threading.Event | None = None,
                    threshold_frames: int = 5) -> bool:
    """Watch for the user talking over the assistant."""
    mic = mic or MIC
    vad = best_vad(CONFIG.voice.vad_energy_floor * 1.6)
    hits = 0
    mic.start()
    try:
        while not (stop_event and stop_event.is_set()):
            f = mic.read(timeout=0.3)
            if f is None:
                continue
            if vad.is_speech(f):
                hits += 1
                if hits >= threshold_frames:
                    return True
            else:
                hits = max(0, hits - 1)
    finally:
        mic.stop()
    return False


def capture_while_held(mic: Microphone | None = None,
                       stop_event: "threading.Event | None" = None,
                       max_seconds: float | None = None,
                       on_level=None) -> Utterance | None:
    """
    Record for exactly as long as the key is held. Nothing more.

    `capture_utterance` decides when you have finished by watching for a pause,
    which costs the VAD silence window (~0.9 s) on every single utterance and
    can keep the microphone open after the key is already up. With push-to-talk
    the user has told us precisely when they are done, so waiting for silence is
    both slower and wrong.

    No VAD is consulted at all here; the key is the only signal.
    """
    mic = mic or MIC
    max_seconds = max_seconds or CONFIG.voice.max_utterance_s
    frames: list[np.ndarray] = []
    frame_s = BLOCK / SR
    started = time.time()

    mic.start()
    mic.drain()                      # discard anything buffered before the press
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if time.time() - started > max_seconds:
                break
            f = mic.read(timeout=0.05)
            if f is None:
                continue
            frames.append(f)
            if on_level is not None:
                on_level(float(np.sqrt(np.mean(np.square(f))) + 1e-9))
    finally:
        mic.stop()

    if not frames:
        return None
    audio = np.concatenate(frames)
    dur = len(audio) / SR
    if dur < 0.20:                   # a stray tap, not speech
        return None
    return Utterance(audio=audio, duration=dur, triggered_by="push_to_talk")
