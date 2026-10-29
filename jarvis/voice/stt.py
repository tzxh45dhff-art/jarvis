"""Speech recognition via mlx-whisper (local, Apple Silicon)."""
from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

import numpy as np

from ..config import CONFIG

log = logging.getLogger("jarvis.stt")


class STTBackend(Protocol):
    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str: ...


class WhisperSTT:
    def __init__(self, model: str | None = None):
        self.model = model or CONFIG.voice.stt_model
        self.fallback = CONFIG.voice.stt_fallback
        self._lock = threading.Lock()
        self._ok: str | None = None

    def _repo(self) -> str:
        return self._ok or self.model

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        import mlx_whisper
        a = np.asarray(audio, dtype=np.float32).reshape(-1)
        if a.size == 0:
            return ""
        peak = float(np.max(np.abs(a))) or 1.0
        if peak > 1.0:
            a = a / peak
        if sample_rate != 16000:
            idx = np.linspace(0, len(a) - 1, int(len(a) * 16000 / sample_rate))
            a = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
        with self._lock:
            for repo in ([self._ok] if self._ok else [self.model, self.fallback]):
                if not repo:
                    continue
                try:
                    t0 = time.time()
                    r = mlx_whisper.transcribe(
                        a, path_or_hf_repo=repo, language="en",
                        condition_on_previous_text=False,
                        temperature=0.0, fp16=True)
                    self._ok = repo
                    txt = (r.get("text") or "").strip()
                    log.info("stt %.2fs (%s): %r", time.time() - t0, repo, txt[:60])
                    return txt
                except Exception as e:
                    log.warning("stt failed with %s: %s", repo, e)
                    continue
        return ""

    def transcribe_file(self, path: str) -> str:
        import soundfile as sf
        a, sr = sf.read(path, dtype="float32")
        if a.ndim > 1:
            a = a.mean(axis=1)
        return self.transcribe(a, sr)

    def warm(self) -> None:
        try:
            self.transcribe(np.zeros(16000, dtype=np.float32), 16000)
        except Exception as e:
            log.warning("stt warmup failed: %s", e)
