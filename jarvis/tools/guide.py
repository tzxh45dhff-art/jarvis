"""
Showing the user where things are, without touching anything.

Every other screen tool either reads or acts. This one guides: the model names
the elements the user needs, in order, and each is drawn on screen as a
numbered outline while the companion cursor flies from one to the next.

Positions come from the same sources `click_text` trusts — OCR for anything
with a visible label, ShowUI for unlabelled icons — never from the reasoning
model's own guess. The companion then snaps each box to the frame of the
control underneath it using the accessibility API, which it can do because it
holds the Accessibility grant and this process does not.

Coordinates are CoreGraphics global points: origin at the top-left of the main
display, y growing downward. OCR only covers the main display, so every box is
on it.
"""
from __future__ import annotations

import time

from ..safety.policy import Risk
from .registry import REGISTRY, ToolResult
from .vision_ground import _grounded_point, _score, ocr_screen

MAX_STEPS = 8

# Stricter than find_on_screen's 0.62. A wrong outline actively misleads —
# the user clicks what they are shown — so a weak text match falls through to
# the visual grounder rather than being drawn.
MIN_TEXT_SCORE = 0.8

# ShowUI answers with a point, not a box. This is the size of the square drawn
# around it until the companion replaces it with the real control frame.
APPROXIMATE_BOX_SIZE = 44


def _best_text_match(element: str, items: list[dict]) -> dict | None:
    best, best_score = None, 0.0
    for item in items:
        score = _score(element, item["text"])
        if score > best_score:
            best, best_score = item, score
    if best is None or best_score < MIN_TEXT_SCORE:
        return None
    return dict(best, score=round(best_score, 3))


@REGISTRY.tool(
    "show_steps",
    "Show the user where to click: draws each element on their screen as a "
    "numbered outline, in order, and flies the companion cursor to each one. "
    "Use when the user asks how to do something, where something is, or to be "
    "walked through an app. Only highlights — it never clicks. Give each "
    "element's visible label exactly as it appears on screen, or describe an "
    "unlabelled control ('the gear icon in the toolbar'). Only include "
    "elements visible right now.",
    {"type": "object", "properties": {
        "steps": {
            "type": "array",
            "description": f"In the order the user should use them, at most {MAX_STEPS}",
            "items": {"type": "object", "properties": {
                "element": {"type": "string",
                            "description": "Visible label, e.g. 'Save', 'Wi-Fi'"},
                "instruction": {"type": "string",
                                "description": "What to do there, a few words, "
                                               "e.g. 'click here'"}},
                "required": ["element", "instruction"]}}},
     "required": ["steps"]},
    risk=Risk.SAFE, category="read", slow=True)
def show_steps(steps: list):
    t0 = time.time()
    if not isinstance(steps, list) or not steps:
        return ToolResult(False, error="steps must be a non-empty list of "
                                       "{element, instruction}.")
    steps = steps[:MAX_STEPS]

    # One OCR pass serves every step: the screen does not change between them,
    # and each pass costs about a quarter of a second.
    items = ocr_screen(force=True)

    drawn: list[dict] = []
    report: list[dict] = []
    for number, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        element = str(step.get("element", "")).strip()
        instruction = str(step.get("instruction", "")).strip()
        if not element:
            continue

        hit = _best_text_match(element, items)
        if hit is not None:
            drawn.append({
                "number": number, "element": element, "instruction": instruction,
                "precision": "exact",
                "rect": {"x": hit["left"], "y": hit["top"],
                         "width": hit["width"], "height": hit["height"]}})
            report.append({"number": number, "element": element,
                           "found": True, "via": "text"})
            continue

        # OCR only sees text. Icons and glyphs need a model trained on GUI
        # layout — the same fallback click_text uses.
        point = _grounded_point(element)
        if point is not None:
            half = APPROXIMATE_BOX_SIZE // 2
            drawn.append({
                "number": number, "element": element, "instruction": instruction,
                "precision": "approximate",
                "rect": {"x": point[0] - half, "y": point[1] - half,
                         "width": APPROXIMATE_BOX_SIZE,
                         "height": APPROXIMATE_BOX_SIZE}})
            # The visual model answers even for things that are not there, so
            # the model must not speak about this step as certain.
            report.append({"number": number, "element": element,
                           "found": True, "via": "visual guess"})
            continue

        report.append({"number": number, "element": element, "found": False})

    missing = [r["element"] for r in report if not r["found"]]
    if not drawn:
        nearby = sorted(items, key=lambda i: -max(
            (_score(m, i["text"]) for m in missing), default=0))[:6]
        return ToolResult(False, error=(
            "None of those elements are visible, so nothing was drawn. "
            "Closest text on screen: "
            + ", ".join(repr(i["text"][:30]) for i in nearby)
            + ". Use the exact visible label, or tell the user in words where "
              "to look if it is not on screen yet."))

    content = {"shown": len(drawn), "steps": report}
    if any(r.get("via") == "visual guess" for r in report):
        content["guesses"] = ("Steps marked 'visual guess' are only drawn if a "
                              "real control is confirmed there, and may not be "
                              "shown. Say what the element looks like so the "
                              "user can find it either way.")
    if missing:
        content["note"] = ("Not on screen, so not drawn: " + ", ".join(missing)
                           + ". Describe those in words.")
    return ToolResult(
        True, content=content,
        display=f"showing {len(drawn)} step{'s' if len(drawn) != 1 else ''} "
                f"on screen ({time.time() - t0:.1f}s)",
        # The companion reads this; the model never sees it (for_model uses
        # content only), which keeps pixel boxes out of the context window.
        meta={"ui": {"kind": "steps", "steps": drawn}})
