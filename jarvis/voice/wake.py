"""
Wake-word detection with openWakeWord's pretrained "hey jarvis" model.

Runs locally on ONNX at roughly 1 ms per 80 ms frame. No audio ever leaves the
machine, and nothing is transcribed until the wake word fires.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from ..config import CONFIG

log = logging.getLogger("jarvis.wake")


class WakeWordDetector:
    CHUNK = 1280            # 80 ms at 16 kHz - openWakeWord's expected frame

    def __init__(self, name: str | None = None, threshold: float | None = None):
        self.name = name or CONFIG.voice.wake_word
        self.threshold = threshold if threshold is not None else CONFIG.voice.wake_threshold
        self._model = None
        self._buf = np.zeros(0, dtype=np.int16)
        self.last_score = 0.0
        self._cooldown = 0

    @staticmethod
    def available() -> bool:
        try:
            import openwakeword
            d = Path(openwakeword.__file__).parent / "resources" / "models"
            return any(d.glob("*.onnx"))
        except Exception:
            return False

    def load(self) -> bool:
        if self._model is not None:
            return True
        try:
            import openwakeword
            from openwakeword.model import Model
            d = Path(openwakeword.__file__).parent / "resources" / "models"
            path = d / f"{self.name}_v0.1.onnx"
            if not path.exists():
                cands = sorted(d.glob(f"*{self.name}*.onnx"))
                if not cands:
                    log.error("wake model %r not found in %s", self.name, d)
                    return False
                path = cands[0]
            self._model = Model(wakeword_models=[str(path)],
                                inference_framework="onnx")
            log.info("wake word loaded: %s", path.name)
            return True
        except Exception as e:
            log.error("wake word unavailable: %s", e)
            return False

    def reset(self) -> None:
        if self._model is not None:
            try:
                self._model.reset()
            except Exception:
                pass
        self._buf = np.zeros(0, dtype=np.int16)
        self._cooldown = 0

    def feed(self, frame: np.ndarray) -> bool:
        """Feed float32 [-1,1] audio at 16 kHz. True when the wake word fires."""
        if not self.load():
            return False
        pcm = np.clip(np.asarray(frame, dtype=np.float32).reshape(-1), -1, 1)
        self._buf = np.concatenate([self._buf, (pcm * 32767).astype(np.int16)])
        fired = False
        while len(self._buf) >= self.CHUNK:
            chunk, self._buf = self._buf[: self.CHUNK], self._buf[self.CHUNK:]
            try:
                scores = self._model.predict(chunk)
            except Exception as e:
                log.warning("wake predict failed: %s", e)
                return False
            score = max(scores.values()) if scores else 0.0
            self.last_score = float(score)
            if self._cooldown > 0:
                self._cooldown -= 1
                continue
            if score >= self.threshold:
                fired = True
                self._cooldown = 25          # ~2 s before it can fire again
                self.reset_scores()
        return fired

    def reset_scores(self) -> None:
        try:
            for k in self._model.prediction_buffer:
                self._model.prediction_buffer[k].clear()
        except Exception:
            pass
