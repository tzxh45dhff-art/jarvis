"""show_steps: resolving guidance steps to on-screen boxes, without a screen or a model."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.tools import REGISTRY
from jarvis.tools import guide


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f"  <- {extra}"))
    return bool(cond)


results = []

# A fake screen: what OCR would report, in screen points, top-left origin.
SCREEN = [
    {"text": "Wi-Fi", "left": 1180, "top": 4, "width": 34, "height": 16,
     "x": 1197, "y": 12, "confidence": 1.0},
    {"text": "Network Settings...", "left": 900, "top": 420, "width": 130, "height": 18,
     "x": 965, "y": 429, "confidence": 1.0},
    {"text": "Save", "left": 600, "top": 700, "width": 40, "height": 18,
     "x": 620, "y": 709, "confidence": 1.0},
]
visual_answers = {}      # element -> (x, y) the stubbed ShowUI returns
visual_queries = []

guide.ocr_screen = lambda force=False, region=None: list(SCREEN)


def fake_grounded_point(query):
    visual_queries.append(query)
    return visual_answers.get(query)


guide._grounded_point = fake_grounded_point


def run(steps):
    visual_queries.clear()
    return guide.show_steps(steps=steps)


# 1. A labelled element resolves to its exact OCR box.
r = run([{"element": "Wi-Fi", "instruction": "click here"}])
ui = r.meta.get("ui", {})
s0 = (ui.get("steps") or [{}])[0]
results.append(check("labelled element found", r.ok, r.error))
results.append(check("exact box from OCR",
                     s0.get("rect") == {"x": 1180, "y": 4, "width": 34, "height": 16}, s0))
results.append(check("marked exact", s0.get("precision") == "exact", s0))
results.append(check("instruction carried", s0.get("instruction") == "click here", s0))
results.append(check("ui payload kind", ui.get("kind") == "steps", ui))
results.append(check("OCR hit skips the visual model", visual_queries == [], visual_queries))

# 2. The model never sees pixel boxes — they go to the companion only.
seen = str(r.for_model())
results.append(check("boxes kept out of the model's context", "rect" not in seen, seen))

# 3. An unlabelled icon falls through to ShowUI and is drawn as approximate.
visual_answers["the gear icon in the toolbar"] = (300, 50)
r = run([{"element": "the gear icon in the toolbar", "instruction": "open settings"}])
s0 = r.meta["ui"]["steps"][0]
results.append(check("icon resolved visually", r.ok and s0["precision"] == "approximate", s0))
results.append(check("approximate box centred on the point",
                     s0["rect"] == {"x": 278, "y": 28, "width": 44, "height": 44}, s0))

# 4. A weak fuzzy text match must not be drawn as if exact.
visual_answers.clear()
r = run([{"element": "Savings account", "instruction": "click"}])
results.append(check("weak text match not drawn", not r.ok, r.meta))
results.append(check("weak match falls through to the visual model",
                     visual_queries == ["Savings account"], visual_queries))

# 5. Numbering survives a missing step, and the model is told what was missing.
r = run([{"element": "Wi-Fi", "instruction": "click the wifi icon"},
         {"element": "Join Other Network", "instruction": "pick this"},
         {"element": "Network Settings", "instruction": "then this"}])
numbers = [s["number"] for s in r.meta["ui"]["steps"]]
results.append(check("partial success is still ok", r.ok, r.error))
results.append(check("numbers match the user's order", numbers == [1, 3], numbers))
results.append(check("missing step reported to the model",
                     "Join Other Network" in r.content.get("note", ""), r.content))

# 6. Nothing visible at all fails with a useful hint instead of drawing nothing.
r = run([{"element": "Bluetooth", "instruction": "click"}])
results.append(check("nothing found is a failure", not r.ok, r))
results.append(check("failure names what is on screen", "Wi-Fi" in r.error, r.error))
results.append(check("failure draws nothing", "ui" not in r.meta, r.meta))

# 7. Bad input.
results.append(check("empty steps rejected", not run([]).ok))
results.append(check("blank element skipped",
                     not run([{"element": "  ", "instruction": "x"}]).ok))

# 8. Step cap.
r = run([{"element": "Save", "instruction": str(i)} for i in range(10)])
results.append(check(f"capped at {guide.MAX_STEPS} steps",
                     len(r.meta["ui"]["steps"]) == guide.MAX_STEPS,
                     len(r.meta["ui"]["steps"])))

# 9. Registered, read-only, and runs without asking for confirmation.
tool = REGISTRY.get("show_steps")
results.append(check("registered", tool is not None))
results.append(check("never gated", REGISTRY.assess("show_steps", {}).needs_confirm is False,
                     REGISTRY.assess("show_steps", {})))
r = REGISTRY.execute("show_steps", {"steps": [{"element": "Save", "instruction": "click"}]})
results.append(check("executes through the registry", r.ok, r.error))

# 10. The chat template records tool-call arguments in alphabetical key order.
# If that differs from the order the model writes them, the cached prompt stops
# matching its re-render and every later turn pays a rebuild. Keep them equal.
props = list(tool.parameters["properties"]["steps"]["items"]["properties"])
results.append(check("argument order is alphabetical", props == sorted(props), props))

print(f"\n{sum(results)}/{len(results)} guide tests passed")
sys.exit(0 if all(results) else 1)
