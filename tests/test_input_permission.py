"""
Mouse and keyboard tools must fail loudly when macOS would drop their events.

Without Accessibility permission macOS discards synthetic input silently, so
every click "succeeded" while nothing on screen changed. These checks run
offline: the permission check and CGEventPost are both stubbed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.tools import REGISTRY
from jarvis.tools import control as C

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


posted = []
Q = C._q()
real_post, real_allowed = Q.CGEventPost, C.input_allowed
Q.CGEventPost = lambda tap, ev: posted.append(ev)
# Pin the "front app" so the test never depends on - or types toward - what is
# actually open on screen. Real events are stubbed out above regardless.
from jarvis.tools import ax as AX
real_front, real_focus, real_ft = C.frontmost_app, C.focus_app, AX.focused_text
C.frontmost_app = lambda fresh=False: "TextEdit"
C.focus_app = lambda app, timeout=3.0: (True, "TextEdit")
AX.focused_text = lambda app: None                      # unreadable: no verify
ACTIONS = [("click", {"x": 10, "y": 10}), ("mouse_move", {"x": 10, "y": 10}),
           ("type_text", {"text": "hello"}), ("press_key", {"keys": "cmd+s"}),
           ("scroll", {"amount": -3}), ("drag", {"x1": 1, "y1": 1, "x2": 5, "y2": 5})]

print("=== permission missing ===")
C.input_allowed = lambda: False
os.environ["__CFBundleIdentifier"] = "com.apple.Terminal"
for name, args in ACTIONS:
    posted.clear()
    r = REGISTRY.execute(name, args, confirmed=True)
    check(f"{name} fails instead of pretending", not r.ok, r)
    check(f"{name} posts no events at all", not posted, len(posted))
r = REGISTRY.execute("type_text", {"text": "x"}, confirmed=True)
check("the error names the app that needs the permission",
      "Terminal needs Accessibility permission" in (r.error or ""), r.error)
check("and says where to turn it on", "Privacy & Security > Accessibility" in r.error)
check("and that nothing happened", "Nothing was clicked or typed" in r.error)

print("\n=== permission granted ===")
C.input_allowed = lambda: True
for name, args in ACTIONS:
    posted.clear()
    r = REGISTRY.execute(name, args, confirmed=True)
    check(f"{name} posts events", r.ok and posted, (r.ok, r.error, len(posted)))

print("\n=== who gets asked ===")
for bid, want in (("com.apple.Terminal", "Terminal"), ("com.googlecode.iterm2", "iTerm"),
                  ("com.example.jarvis.companion", "JARVIS Companion")):
    os.environ["__CFBundleIdentifier"] = bid
    check(f"{bid} -> {want}", C.launcher_app() == want, C.launcher_app())
os.environ.pop("__CFBundleIdentifier")
os.environ["TERM_PROGRAM"] = "Apple_Terminal"
check("falls back to TERM_PROGRAM", C.launcher_app() == "Terminal")

print("\n=== the real check ===")
Q.CGEventPost, C.input_allowed = real_post, real_allowed
C.frontmost_app, C.focus_app, AX.focused_text = real_front, real_focus, real_ft
check("asks macOS without raising", isinstance(C.input_allowed(), bool))

print(f"\n{sum(R)}/{len(R)} input-permission checks passed")
sys.exit(0 if all(R) else 1)
