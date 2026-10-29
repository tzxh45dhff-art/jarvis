"""
Voice activity detection.

Prefers Silero VAD (an ONNX model that arrives with openWakeWord, so no extra
download) and falls back to an adaptive energy gate if it is unavailable.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger("jarvis.vad")


class EnergyVAD:
    """Adaptive noise-floor gate. Robust, no model needed."""

    def __init__(self, floor: float = 0.012):
        self.base = floor
        self.noise = floor

    def is_speech(self, frame: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(np.square(frame))) + 1e-9)
        speech = rms > max(self.base, self.noise * 2.8)
        if not speech:
            # track the ambient level so a noisy room does not gate everything
            self.noise = 0.97 * self.noise + 0.03 * rms
        return speech

    def probability(self, frame: np.ndarray) -> float:
        return 1.0 if self.is_speech(frame) else 0.0

    def reset(self) -> None:
        self.noise = self.base


class SileroVAD:
    """ONNX Silero VAD; roughly 1 ms per 32 ms frame on CPU."""

    FRAME = 512          # samples at 16 kHz

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self._sess = None
        self._names: set[str] = set()
        self._failed = False
        self.reset()

    @staticmethod
    def model_path() -> Path | None:
        try:
            import openwakeword
            p = (Path(openwakeword.__file__).parent / "resources" / "models"
                 / "silero_vad.onnx")
            return p if p.exists() else None
        except Exception:
            return None

    def load(self) -> bool:
        if self._sess is not None:
            return True
        if self._failed:
            return False
        p = self.model_path()
        if p is None:
            self._failed = True
            return False
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            opts.log_severity_level = 4
            self._sess = ort.InferenceSession(str(p), sess_options=opts,
                                              providers=["CPUExecutionProvider"])
            self._names = {i.name for i in self._sess.get_inputs()}
            self.reset()
            return True
        except Exception as e:
            log.warning("silero unavailable: %s", e)
            self._failed = True
            return False

    def reset(self) -> None:
        # Silero ships in two shapes: v3 takes separate h/c LSTM states,
        # v5 takes a single packed `state`. Support both.
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._buf = np.zeros(0, dtype=np.float32)
        self._last = 0.0

    def probability(self, frame: np.ndarray) -> float:
        if not self.load():
            return self._last
        self._buf = np.concatenate(
            [self._buf, np.asarray(frame, dtype=np.float32).reshape(-1)])
        while len(self._buf) >= self.FRAME:
            chunk = self._buf[: self.FRAME]
            self._buf = self._buf[self.FRAME:]
            feed: dict = {"input": chunk.reshape(1, -1).astype(np.float32)}
            if "sr" in self._names:
                feed["sr"] = np.array(16000, dtype=np.int64)
            if {"h", "c"} <= self._names:
                feed["h"], feed["c"] = self._h, self._c
            elif "state" in self._names:
                feed["state"] = self._state
            try:
                out = self._sess.run(None, feed)
            except Exception as e:
                log.warning("silero inference failed: %s", e)
                self._sess = None
                self._failed = True          # fall back rather than spam
                return self._last
            self._last = float(np.asarray(out[0]).reshape(-1)[0])
            if {"h", "c"} <= self._names and len(out) >= 3:
                self._h = np.asarray(out[1], dtype=np.float32)
                self._c = np.asarray(out[2], dtype=np.float32)
            elif "state" in self._names and len(out) > 1:
                self._state = np.asarray(out[1], dtype=np.float32)
        return self._last

    def is_speech(self, frame: np.ndarray) -> bool:
        return self.probability(frame) >= self.threshold


class HybridVAD:
    """Silero when it works, energy gate the moment it does not."""

    def __init__(self, energy_floor: float = 0.012, threshold: float = 0.5):
        self.silero = SileroVAD(threshold)
        self.energy = EnergyVAD(energy_floor)
        self.using_silero = self.silero.load()

    def is_speech(self, frame: np.ndarray) -> bool:
        if self.using_silero:
            p = self.silero.probability(frame)
            if self.silero._sess is None:        # session died mid-stream
                self.using_silero = False
            else:
                return p >= self.silero.threshold
        return self.energy.is_speech(frame)

    def probability(self, frame: np.ndarray) -> float:
        return (self.silero.probability(frame) if self.using_silero
                else self.energy.probability(frame))

    def reset(self) -> None:
        self.silero.reset()
        self.energy.reset()


def best_vad(energy_floor: float = 0.012):
    v = HybridVAD(energy_floor)
    log.info("VAD backend: %s", "silero" if v.using_silero else "energy")
    return v
