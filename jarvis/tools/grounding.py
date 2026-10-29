"""
ShowUI-2B: a model whose only job is answering "where is X on screen".

OCR handles anything with a text label, exactly and cheaply. What it cannot see
is the large part of a UI that carries no text — the send arrow, the hamburger
menu, a play triangle, an avatar, a toolbar icon. Asking the main reasoning
model for those coordinates does not work; it has no metric grounding and simply
guesses.

ShowUI is fine-tuned on GUI screenshots with coordinate supervision and returns
a normalised [x, y] point. At 2B parameters and ~2 GB it sits alongside Gemma
rather than replacing anything, and it is only consulted when OCR has already
failed, so it costs nothing on the common path.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("jarvis.grounding")

MODEL = "mlx-community/ShowUI-2B-bf16-4bit"

# From the model card: ShowUI is trained with this exact framing, and it matters —
# the coordinate convention is part of the prompt.
SYSTEM = ("Based on the screenshot of the page, I give a text description and "
          "you give its corresponding location. The coordinate represents a "
          "clickable location [x, y] for an element, which is a relative "
          "coordinate on the screenshot, scaled from 0 to 1.")

MIN_PIXELS = 256 * 28 * 28
MAX_PIXELS = 1344 * 28 * 28

_POINT = re.compile(r"\[?\s*([01]?\.\d+|[01])\s*,\s*([01]?\.\d+|[01])\s*\]?")


class ShowUIGrounder:
    def __init__(self, model_id: str = MODEL):
        self.model_id = model_id
        self._model = None
        self._processor = None
        self._config = None
        self._lock = threading.RLock()
        self._failed = False

    @property
    def available(self) -> bool:
        if self._failed:
            return False
        if self._model is not None:
            return True
        from ..core.llm import _is_cached
        return _is_cached(self.model_id)

    def load(self) -> bool:
        with self._lock:
            if self._model is not None:
                return True
            if self._failed:
                return False
            try:
                from mlx_vlm import load as vlm_load
                from mlx_vlm.utils import load_config
                t0 = time.time()
                self._model, self._processor = vlm_load(
                    self.model_id,
                    processor_kwargs={"min_pixels": MIN_PIXELS,
                                      "max_pixels": MAX_PIXELS})
                self._config = load_config(self.model_id)
                log.info("ShowUI loaded in %.1fs", time.time() - t0)
                return True
            except Exception as e:
                log.warning("ShowUI unavailable: %s", e)
                self._failed = True
                return False

    def locate(self, query: str, image: str | Path) -> tuple[float, float] | None:
        """Return a normalised (x, y) in 0..1, or None if it could not answer."""
        if not self.load():
            return None
        from mlx_vlm import generate as vlm_generate
        with self._lock:
            try:
                # Ordering matters and is part of how ShowUI was trained:
                # system text, THEN the image, THEN the query. Folding the query
                # into one text block makes it answer a constant point for every
                # input — measured 2/11 that way versus 5/5 like this.
                messages = [{"role": "user", "content": [
                    {"type": "text", "text": SYSTEM},
                    {"type": "image"},
                    {"type": "text", "text": query},
                ]}]
                prompt = self._processor.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True)
                out = vlm_generate(self._model, self._processor, prompt,
                                   image=[str(image)], max_tokens=48,
                                   temperature=0.0, verbose=False)
            except Exception as e:
                log.warning("ShowUI generate failed: %s", e)
                return None
        text = getattr(out, "text", out)
        m = _POINT.search(str(text))
        if not m:
            log.debug("ShowUI gave no point for %r: %r", query, str(text)[:120])
            return None
        x, y = float(m.group(1)), float(m.group(2))
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            return None
        return x, y

    def unload(self) -> None:
        with self._lock:
            self._model = self._processor = self._config = None
            try:
                import mlx.core as mx
                mx.clear_cache()
            except Exception:
                pass


_GROUNDER: ShowUIGrounder | None = None


def get_grounder() -> ShowUIGrounder:
    global _GROUNDER
    if _GROUNDER is None:
        _GROUNDER = ShowUIGrounder()
    return _GROUNDER
