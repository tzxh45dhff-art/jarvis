"""
Locating things on screen precisely.

The whole point of this module: a vision-language model cannot be trusted to
produce pixel coordinates. Asked where a button is, it guesses, and the guess is
tens of pixels out — which shows up as an agent clicking (473, 803), then
(465, 803), then (472, 814), groping around and never hitting anything.

So coordinates are never guessed. They come from two sources that actually know:

  1. The accessibility tree, which reports exact frames — but is empty for
     Electron and Catalyst apps (WhatsApp reports precisely one element: its
     own window title).
  2. Apple's Vision OCR, which returns a bounding box for every piece of text
     on screen. It works everywhere, including inside Electron, and takes about
     a quarter of a second for a full screen.

`find_on_screen` and `click_text` are what the model should reach for; raw
`click(x, y)` remains available but is a last resort.
"""
from __future__ import annotations

import difflib
import logging
import re
import subprocess
import time
from pathlib import Path

from ..safety.policy import Risk, Verdict
from .registry import REGISTRY, ToolResult

log = logging.getLogger("jarvis.ground")

_CACHE: dict = {"at": 0.0, "items": [], "size": (0, 0)}
CACHE_TTL = 1.2          # a screen does not change between two calls in a row


def _screen_points() -> tuple[int, int]:
    import Quartz
    d = Quartz.CGMainDisplayID()
    return int(Quartz.CGDisplayPixelsWide(d)), int(Quartz.CGDisplayPixelsHigh(d))


def ocr_screen(force: bool = False, region: tuple | None = None) -> list[dict]:
    """
    Every piece of text on screen, with its centre in *screen points*.

    Vision reports normalised, bottom-left-origin boxes; the mouse works in
    top-left-origin points, and the capture is in backing pixels. Both
    conversions happen here so callers never deal with coordinate spaces.
    """
    now = time.time()
    if not force and region is None and now - _CACHE["at"] < CACHE_TTL:
        return _CACHE["items"]

    import Quartz
    import Vision
    from Foundation import NSURL

    from .screen import SHOT_DIR, _capture

    shot = SHOT_DIR / f"ocr_{int(now * 1000)}.png"
    try:
        _capture(shot, region=region)
    except Exception as e:
        log.warning("ocr capture failed: %s", e)
        return []

    url = NSURL.fileURLWithPath_(str(shot))
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        return []
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if img is None:
        return []
    px_w = Quartz.CGImageGetWidth(img)
    px_h = Quartz.CGImageGetHeight(img)
    pt_w, pt_h = _screen_points()
    # A Retina capture is 2x the point size; region captures are already points.
    if region is not None:
        rx, ry, rw, rh = region
        sx, sy = rw / px_w, rh / px_h
        ox, oy = rx, ry
    else:
        sx, sy = pt_w / px_w, pt_h / px_h
        ox = oy = 0

    items: list[dict] = []

    def handler(request, error):
        for obs in request.results() or []:
            cand = obs.topCandidates_(1)
            if not cand:
                continue
            text = cand[0].string()
            bb = obs.boundingBox()          # normalised, origin bottom-left
            bx, by = bb.origin.x, bb.origin.y
            bw, bh = bb.size.width, bb.size.height
            left = bx * px_w
            width = bw * px_w
            # flip the y axis: Vision counts up from the bottom
            top = (1.0 - by - bh) * px_h
            height = bh * px_h
            items.append({
                "text": text,
                "confidence": float(cand[0].confidence()),
                "x": int(ox + (left + width / 2) * sx),
                "y": int(oy + (top + height / 2) * sy),
                "left": int(ox + left * sx),
                "top": int(oy + top * sy),
                "width": int(width * sx),
                "height": int(height * sy),
            })

    req = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(handler)
    req.setRecognitionLevel_(0)             # 0 = accurate
    req.setUsesLanguageCorrection_(False)   # UI labels are not prose
    handler_obj = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
        img, None)
    ok, err = handler_obj.performRequests_error_([req], None)
    try:
        shot.unlink(missing_ok=True)
    except Exception:
        pass
    if not ok:
        log.warning("ocr failed: %s", err)
        return []
    if region is None:
        _CACHE.update(at=now, items=items, size=(pt_w, pt_h))
    return items


def _score(needle: str, hay: str) -> float:
    n, h = needle.lower().strip(), hay.lower().strip()
    if not n or not h:
        return 0.0
    if n == h:
        return 1.0
    if h.startswith(n) or h.endswith(n):
        return 0.94
    if n in h:
        return 0.88
    if n in re.split(r"\W+", h):
        return 0.9
    return difflib.SequenceMatcher(None, n, h).ratio()


def locate(text: str, min_score: float = 0.62,
           force: bool = False) -> list[dict]:
    items = ocr_screen(force=force)
    scored = []
    for it in items:
        s = _score(text, it["text"])
        if s >= min_score:
            scored.append(dict(it, score=round(s, 3)))
    scored.sort(key=lambda d: -d["score"])
    return scored


@REGISTRY.tool(
    "find_on_screen",
    "Find text on screen and get its exact clickable coordinates. Use this "
    "BEFORE clicking anything: it reads the screen with OCR and returns real "
    "positions, so you never have to guess where something is. Works in every "
    "app including Electron ones like WhatsApp, Slack and VS Code.",
    {"type": "object", "properties": {
        "text": {"type": "string",
                 "description": "The visible label to find, e.g. 'Send', 'Dad', 'Sign in'"},
        "all_text": {"type": "boolean",
                     "description": "Return every text item on screen instead of matching"},
        "limit": {"type": "integer"}},
     "required": []},
    risk=Risk.SAFE, category="read", slow=True)
def find_on_screen(text: str = "", all_text: bool = False, limit: int = 25):
    t0 = time.time()
    if all_text or not text:
        items = ocr_screen(force=True)
        rows = [{"text": i["text"], "x": i["x"], "y": i["y"]} for i in items][:limit]
        return ToolResult(True, content={"count": len(items), "items": rows},
                          display=f"read {len(items)} text items "
                                  f"({time.time() - t0:.1f}s)")
    hits = locate(text, force=True)
    if not hits:
        near = sorted(ocr_screen(), key=lambda i: -_score(text, i["text"]))[:6]
        return ToolResult(False, error=(
            f"No text matching {text!r} is visible. Closest things on screen: "
            + ", ".join(repr(i["text"][:30]) for i in near)
            + ". Use find_on_screen with all_text=true to see everything, or "
              "scroll and look again."))
    rows = [{"text": h["text"], "x": h["x"], "y": h["y"], "score": h["score"]}
            for h in hits[:limit]]
    return ToolResult(True, content={"query": text, "matches": rows},
                      display=f"found {text!r} at ({rows[0]['x']}, {rows[0]['y']})")


def _grounded_point(query: str) -> tuple[int, int] | None:
    """Ask ShowUI where something is, in screen points. None if unavailable."""
    try:
        from .grounding import get_grounder
        g = get_grounder()
        if not g.available:
            return None
        from .screen import SHOT_DIR, _capture, _downscale
        shot = SHOT_DIR / f"ground_{int(time.time() * 1000)}.png"
        _capture(shot)
        # A raw Retina capture is ~3024 px wide and the vision tower spends
        # seconds just resizing it. ShowUI's own max_pixels is about 1 MP, so
        # feeding it anything larger is wasted work.
        small = _downscale(shot, 1280)
        norm = g.locate(query, small)
        try:
            shot.unlink(missing_ok=True)
        except Exception:
            pass
        if norm is None:
            return None
        w, h = _screen_points()
        return int(norm[0] * w), int(norm[1] * h)
    except Exception as e:
        log.warning("visual grounding failed: %s", e)
        return None


def _click_verdict(args: dict) -> Verdict:
    from .control import _control_verdict
    return _control_verdict({"keys": ""})


@REGISTRY.tool(
    "click_text",
    "Click on visible text by name — the reliable way to click anything. "
    "Locates the text with OCR first, then clicks its exact centre. Prefer this "
    "over click(x, y): guessing coordinates from a screenshot does not work.",
    {"type": "object", "properties": {
        "text": {"type": "string",
                 "description": "The label to click, or a description of an "
                                "unlabelled control like 'the send arrow icon'"},
        "occurrence": {"type": "integer",
                       "description": "Which match if several, 1-based (default 1)"},
        "double": {"type": "boolean"},
        "right": {"type": "boolean"}},
     "required": ["text"]},
    risk=Risk.MEDIUM, category="ui_input", assess=_click_verdict,
    parallel_safe=False, slow=True)
def click_text(text: str, occurrence: int = 1, double: bool = False,
               right: bool = False):
    from .control import click as raw_click
    hits = locate(text, force=True)
    if not hits:
        # OCR only sees text. Icons, glyphs and unlabelled controls need a model
        # trained on GUI layout, so fall through to ShowUI before giving up.
        pt = _grounded_point(text)
        if pt is not None:
            from .control import click as raw_click
            res = raw_click(x=pt[0], y=pt[1], button="right" if right else "left",
                            double=double)
            if not res.ok:
                return res
            return ToolResult(True, content={"clicked": text, "x": pt[0],
                                             "y": pt[1], "via": "showui"},
                              display=f"clicked {text!r} at ({pt[0]}, {pt[1]}) "
                                      f"(visual match)")
        return find_on_screen(text=text)
    idx = max(1, occurrence) - 1
    if idx >= len(hits):
        return ToolResult(False, error=(
            f"Only {len(hits)} match(es) for {text!r}; asked for #{occurrence}."))
    h = hits[idx]
    res = raw_click(x=h["x"], y=h["y"], button="right" if right else "left",
                    double=double)
    if not res.ok:
        return res
    return ToolResult(True, content={"clicked": h["text"], "x": h["x"],
                                     "y": h["y"], "score": h["score"],
                                     "matches": len(hits)},
                      display=f"clicked {h['text'][:40]!r} at ({h['x']}, {h['y']})")


@REGISTRY.tool(
    "type_into",
    "Find a field by the text visible on screen, click it and type. A fallback "
    "for apps whose fields do not appear in ui_elements - normally use "
    "set_field (apps) or browser_type (web pages), which are exact.",
    {"type": "object", "properties": {
        "field": {"type": "string",
                  "description": "Visible label or placeholder of the field"},
        "text": {"type": "string", "description": "What to type"},
        "clear": {"type": "boolean", "description": "Select-all and replace first"},
        "submit": {"type": "boolean",
                   "description": "Press Return afterwards (gated in messaging apps)"}},
     "required": ["field", "text"]},
    risk=Risk.MEDIUM, category="ui_input", assess=_click_verdict,
    parallel_safe=False, slow=True)
def type_into(field: str, text: str, clear: bool = False, submit: bool = False):
    from .control import press_key, type_text
    r = click_text(field)
    if not r.ok:
        return r
    time.sleep(0.25)
    if clear:
        press_key(keys="cmd+a", quiet=True)
        time.sleep(0.08)
    tr = type_text(text=text)
    if not tr.ok:
        return tr

    # Prove the text landed. Synthetic keystrokes into a virtualised editor
    # (Monaco, CodeMirror, some Electron fields) are frequently swallowed, and
    # reporting "typed 586 characters" into an untouched editor is worse than
    # failing — the caller builds its next step on a lie.
    time.sleep(0.35)
    verified, seen = _verify_typed(text)
    out = {"field": field, "typed": len(text), "verified": verified}

    if submit and verified:
        time.sleep(0.15)
        sr = REGISTRY.execute("press_key", {"keys": "return"})
        out["submitted"] = sr.ok
        if not sr.ok:
            return ToolResult(True, content=out,
                              display=f"typed into {field!r}; not submitted: "
                                      f"{sr.error[:60]}")
    if not verified:
        return ToolResult(False, content=out, error=(
            f"Typed {len(text)} characters into {field!r} but the text is not "
            f"on screen afterwards, so it did not land. "
            + (f"What is there instead: {seen[:120]!r}. " if seen else "")
            + "This field is probably a rich editor that ignores synthetic "
              "keystrokes — set its contents through the app or the DOM "
              "instead (browser_js / browser_type)."))
    return ToolResult(True, content=out,
                      display=f"typed {len(text)} chars into {field!r}"
                              + (" and submitted" if submit else "")
                              + " (verified on screen)")


def _verify_typed(text: str) -> tuple[bool, str]:
    """
    Confirm typed text is actually visible.

    Compares against a distinctive fragment rather than the whole string: an
    editor may wrap, re-indent or autocomplete what was typed, so demanding an
    exact full match would reject text that genuinely arrived.
    """
    probe = _distinctive_fragment(text)
    if not probe:
        return True, ""          # nothing checkable (e.g. only whitespace)
    items = ocr_screen(force=True)
    haystack = " ".join(i["text"] for i in items).lower()
    if probe.lower() in haystack:
        return True, ""
    # Try a couple of shorter fragments before declaring failure — OCR can
    # split a long token across two boxes.
    for frag in _fragments(text):
        if frag.lower() in haystack:
            return True, ""
    near = " ".join(i["text"] for i in items[:14])
    return False, near


def _distinctive_fragment(text: str) -> str:
    words = [w for w in re.split(r"\s+", text.strip()) if len(w) >= 4]
    return max(words, key=len) if words else text.strip()[:12]


def _fragments(text: str) -> list[str]:
    words = [w for w in re.split(r"\s+", text.strip()) if len(w) >= 3]
    return sorted(words, key=len, reverse=True)[:4]
