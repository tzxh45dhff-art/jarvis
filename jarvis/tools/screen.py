"""
Screen capture and visual understanding.

Screenshots are downscaled before they reach the vision tower: a Retina capture
is ~3024px wide, and feeding that costs seconds for no accuracy gain.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from ..config import DATA_DIR
from ..safety.policy import Risk
from .registry import REGISTRY, ToolResult

SHOT_DIR = DATA_DIR / "screenshots"
SHOT_DIR.mkdir(parents=True, exist_ok=True)
TARGET_WIDTH = 1280          # measured: same OCR accuracy as 1512, ~1.8x faster
DETAIL_WIDTH = 1890         # for genuinely small text


class ScreenPermissionError(RuntimeError):
    pass


def _capture(path: Path, window_id: str | None = None,
             region: tuple[int, int, int, int] | None = None) -> Path:
    cmd = ["screencapture", "-x"]          # -x = no shutter sound
    if window_id:
        cmd += ["-l", str(window_id)]
    elif region:
        cmd += ["-R", ",".join(str(int(v)) for v in region)]
    cmd += ["-t", "png", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if not path.exists() or path.stat().st_size == 0:
        err = (r.stderr or "").strip()
        raise ScreenPermissionError(
            "Screen capture failed"
            + (f": {err}" if err else "")
            + ". macOS requires Screen Recording permission for the app running "
              "JARVIS (System Settings > Privacy & Security > Screen Recording). "
              "Grant it to Terminal, then restart Terminal."
        )
    return path


def _downscale(path: Path, width: int = TARGET_WIDTH) -> Path:
    try:
        from PIL import Image
        img = Image.open(path)
        if img.width <= width:
            return path
        h = int(img.height * width / img.width)
        out = path.with_name(path.stem + "_s.png")
        img.convert("RGB").resize((width, h), Image.LANCZOS).save(out, "PNG")
        return out
    except Exception:
        return path


def take_screenshot(region: tuple | None = None, detail: bool = False) -> Path:
    p = SHOT_DIR / f"shot_{int(time.time() * 1000)}.png"
    _capture(p, region=region)
    return _downscale(p, DETAIL_WIDTH if detail else TARGET_WIDTH)


@REGISTRY.tool(
    "screenshot",
    "Capture the screen and return the image path plus its dimensions. "
    "Use see_screen instead if you want the contents described.",
    {"type": "object", "properties": {
        "region": {"type": "string",
                   "description": "Optional 'x,y,w,h' to capture part of the screen"}},
     "required": []},
    risk=Risk.SAFE, category="read", slow=True)
def screenshot(region: str = ""):
    try:
        reg = tuple(int(v) for v in region.split(",")) if region else None
        p = take_screenshot(reg)
    except ScreenPermissionError as e:
        return ToolResult(False, error=str(e))
    except Exception as e:
        return ToolResult(False, error=f"capture failed: {e}")
    try:
        from PIL import Image
        w, h = Image.open(p).size
    except Exception:
        w = h = 0
    return ToolResult(True, content={"path": str(p), "width": w, "height": h},
                      display=f"captured screen ({w}x{h})")


def _ground(region) -> tuple[dict, list[dict]]:
    """Facts no vision model has to guess: front app, window, dialog, and every
    piece of on-screen text with its real clickable position."""
    ctx: dict = {}
    try:
        from .control import observe
        ctx = observe(0)
    except Exception:
        pass
    texts: list[dict] = []
    try:
        from .vision_ground import ocr_screen
        items = ocr_screen(force=True, region=region)
        items = sorted(items, key=lambda i: (round(i["y"] / 12), i["x"]))
        texts = [{"text": i["text"][:80], "x": int(i["x"]), "y": int(i["y"])}
                 for i in items if i.get("text", "").strip()][:80]
    except Exception:
        pass
    return ctx, texts


# Questions about how something *looks* need the picture; everything else
# (what is open, what does it say, is there a button called X) is answered
# exactly by the recognised text and accessibility facts - without the extra
# vision-model round trip, which measured 3.7 s of a 3.7 s call.
_VISUAL = re.compile(
    r"\b(colou?r\w*|icon|image|picture|photo|logo|avatar|emoji|chart|graph|diagram|"
    r"looks?|looking|appearance|layout|design|highlight\w*|toggle\w*|checkbox|"
    r"switch|slider|badge|thumbnail|video|animation|shape|arrow|visual\w*|"
    r"describe|what is this|picture|handwrit\w*|scribbl\w*|signature|sketch\w*|"
    r"diagram|drawing|whiteboard|note ?book)\b", re.I)
# Handwriting and small print need the sharper capture as well as the vision
# model: a 1280-px screenshot of two pages side by side leaves each page ~640 px.
_FINE = re.compile(r"\b(handwrit\w*|scribbl\w*|signature|small print|fine print|"
                   r"two (documents|pages|images)|side by side|both (documents|pages|images))\b", re.I)
_MIN_TEXT_ITEMS = 4          # fewer than this and the screen is mostly graphics


def _text_summary(ctx: dict, texts: list[dict]) -> str:
    parts = []
    if ctx.get("front_app"):
        parts.append(f"{ctx['front_app']} is in front"
                     + (f", window '{ctx['window']}'" if ctx.get("window") else "") + ".")
    if ctx.get("dialog"):
        parts.append(f"A dialog or sheet is open: {ctx['dialog']}.")
    if texts:
        parts.append("Text on screen, top to bottom: "
                     + " | ".join(t["text"] for t in texts[:60]))
    else:
        parts.append("No readable text was found.")
    return " ".join(parts)


@REGISTRY.tool(
    "see_screen",
    "Look at the screen. By default this is FAST (about half a second): it "
    "returns the front app, window, any open dialog, and every piece of text on "
    "screen with its real click position - enough to read errors, find buttons "
    "and verify what happened. Set vision=true only when you need to know how "
    "something LOOKS (an icon, an image, colours, layout), which costs several "
    "seconds more.",
    {"type": "object", "properties": {
        "question": {"type": "string",
                     "description": "What to determine, e.g. 'what error is shown?'"},
        "region": {"type": "string", "description": "Optional 'x,y,w,h'"},
        "detail": {"type": "boolean",
                   "description": "Capture at higher resolution for very small text"},
        "vision": {"type": "boolean",
                   "description": "Also run the vision model (slower); for "
                                  "visual questions only"}},
     "required": ["question"]},
    risk=Risk.SAFE, category="read", slow=True)
def see_screen(question: str, region: str = "", detail: bool = False,
               vision: bool = False):
    if _FINE.search(question or ""):
        detail = True
        vision = True
    try:
        reg = tuple(int(v) for v in region.split(",")) if region else None
        p = take_screenshot(reg, detail=detail)
    except ScreenPermissionError as e:
        return ToolResult(False, error=str(e))
    except Exception as e:
        return ToolResult(False, error=f"capture failed: {e}")
    # Ground first: the text recognised on screen and the accessibility facts
    # are exact, so they answer most questions and, when the vision model is
    # used, go in its prompt and come back with real click coordinates.
    ctx, texts = _ground(reg)
    use_vision = bool(vision) or bool(_VISUAL.search(question or "")) \
        or len(texts) < _MIN_TEXT_ITEMS
    if not use_vision:
        content = {"observation": _text_summary(ctx, texts), "screenshot": str(p),
                   "mode": "text", **ctx}
        if texts:
            content["text_on_screen"] = texts
            content["how_to_click"] = (
                "to click any text listed above use click_text with that text, "
                "or click with its x,y (real screen points); prefer press_element "
                "when the app lists its controls in ui_elements")
        content["note"] = ("answered from on-screen text; call see_screen with "
                           "vision=true if you need to know how something looks")
        return ToolResult(True, content=content, display="read the screen"
                          + (f" ({ctx['front_app']})" if ctx.get("front_app") else ""))

    from ..core.llm import get_backend
    facts = []
    if ctx.get("front_app"):
        facts.append(f"App in front: {ctx['front_app']}"
                     + (f" - window '{ctx['window']}'" if ctx.get("window") else ""))
    if ctx.get("dialog"):
        facts.append(f"A dialog or sheet is open: {ctx['dialog']}")
    if texts:
        facts.append("Text recognised on screen (exact): "
                     + " | ".join(t["text"] for t in texts[:60]))
    prompt = (
        "You are looking at a macOS screen capture. Answer precisely and only from "
        "what is visible. If you cannot see something, say so. Do not estimate "
        "pixel coordinates - this image is scaled, so they would be wrong.\n\n"
        + ("Facts about this screen (reliable - prefer them over your reading "
           "of small text):\n" + "\n".join(facts) + "\n\n" if facts else "")
        + f"Question: {question}"
    )
    try:
        answer = get_backend().describe_images(prompt, [str(p)], max_tokens=600)
    except Exception as e:
        return ToolResult(False, error=f"vision failed: {e}")
    content = {"observation": answer, "screenshot": str(p), "mode": "vision", **ctx}
    if texts:
        content["text_on_screen"] = texts
        content["how_to_click"] = ("to click any text listed above use click_text "
                                   "with that text, or click with its x,y (real "
                                   "screen points); prefer press_element when the "
                                   "app lists its controls in ui_elements")
    return ToolResult(True, content=content, display="looked at the screen"
                      + (f" ({ctx['front_app']})" if ctx.get("front_app") else ""))


@REGISTRY.tool(
    "look_at_image",
    "Describe or answer a question about an image file on disk.",
    {"type": "object", "properties": {
        "path": {"type": "string"}, "question": {"type": "string"}},
     "required": ["path"]},
    risk=Risk.SAFE, category="read", slow=True)
def look_at_image(path: str, question: str = "Describe this image in detail."):
    from ..core.llm import get_backend
    p = Path(path).expanduser()
    if not p.exists():
        return ToolResult(False, error=f"Image not found: {p}")
    # Documents and handwriting lose detail at screen-preview size.
    small = _downscale(p, DETAIL_WIDTH)
    try:
        answer = get_backend().describe_images(question, [str(small)], max_tokens=900)
    except Exception as e:
        return ToolResult(False, error=f"vision failed: {e}")
    return ToolResult(True, content={"observation": answer, "image": str(p)},
                      display=f"examined {p.name}")
