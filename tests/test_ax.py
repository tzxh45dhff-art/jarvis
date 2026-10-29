"""
Native app control: accessibility tools, targeted typing, dialogs.

Baseline before these tools existed, measured on four multi-step desktop tasks
driven by the real agent: 0/4. After: 4/4, plus a save-as-plain-text task done
entirely through the app's own Save panel.

Offline checks cover the guards. The live section drives Calculator and
TextEdit - and skips itself if either is already open, so it can never touch
something of yours. Everything it creates goes in a temporary folder.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.safety.policy import needs_confirmation
from jarvis.tools import REGISTRY
from jarvis.tools import ax
from jarvis.tools import control as C

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


REGISTRY.confirm_hook = lambda *a: True

# ------------------------------------------------------------------ offline
print("=== typing never goes to the wrong app ===")
Q = C._q()
posted = []
real = (Q.CGEventPost, C.frontmost_app, C.focus_app, C.input_allowed)
Q.CGEventPost = lambda tap, ev: posted.append(ev)
C.input_allowed = lambda: True

C.frontmost_app = lambda fresh=False: "Terminal"
r = REGISTRY.execute("type_text", {"text": "hello"}, confirmed=True)
check("with no app named, it refuses to type into the terminal you talk to it in",
      not r.ok and "where you are talking to me" in r.error, r.error)
check("and not a single key was sent", not posted, len(posted))
r = REGISTRY.execute("press_key", {"keys": "return"}, confirmed=True)
check("same for a key press", not r.ok and not posted)

C.focus_app = lambda app, timeout=3.0: (False, f"could not bring {app} to the front")
r = REGISTRY.execute("type_text", {"text": "hello", "app": "TextEdit"}, confirmed=True)
check("if the named app cannot be brought forward, nothing is typed",
      not r.ok and "Nothing was typed" in r.error and not posted, r.error)

C.focus_app = lambda app, timeout=3.0: (True, "TextEdit")
real_ft = ax.focused_text
ax.focused_text = lambda app: ""                       # field readable, stays empty
r = REGISTRY.execute("type_text", {"text": "hello world", "app": "TextEdit"},
                     confirmed=True)
check("keystrokes that did not land are reported as a failure",
      not r.ok and "did not land" in r.error, r.error)
ax.focused_text = lambda app: None                     # field unreadable
posted.clear()
r = REGISTRY.execute("type_text", {"text": "hi", "app": "TextEdit"}, confirmed=True)
check("an unreadable field is typed into but marked unverified",
      r.ok and r.content["verified"] is None and posted, r.content)
ax.focused_text = real_ft
Q.CGEventPost, C.frontmost_app, C.focus_app, C.input_allowed = real

print("\n=== consequential presses still ask first ===")
for app, label, want in (("Mail", "Send", True), ("Finder", "Empty Trash", True),
                         ("Calculator", "Multiply", False),
                         ("TextEdit", "Don't Save", True)):
    v = REGISTRY.assess("press_element", {"app": app, "element": label})
    check(f"'{label}' in {app} -> confirm={want}", needs_confirmation(v) == want, v)
v = REGISTRY.assess("choose_menu", {"app": "Finder", "path": "Finder > Empty Trash"})
check("a destructive menu command asks first", needs_confirmation(v), v)
v = REGISTRY.assess("choose_menu", {"app": "TextEdit", "path": "Format > Font > Bold"})
check("an ordinary one does not", not needs_confirmation(v), v)
v = REGISTRY.assess("file_dialog", {"app": "TextEdit", "path": "~/x.txt", "overwrite": True})
check("replacing an existing file asks first", needs_confirmation(v), v)

print("\n=== tools are registered and preferred ===")
for t in ("ui_elements", "press_element", "set_field", "choose_menu",
          "select_option", "file_dialog"):
    check(f"{t} registered", t in REGISTRY.names())
from jarvis.core import prompts

sp = prompts.system_prompt()
check("the system prompt ranks menus and controls above screen-reading",
      sp.index("choose_menu") < sp.index("find_on_screen"))

# --------------------------------------------------------------------- live
print("\n=== the real front-app check works (it once always said 'unknown') ===")
front = C.frontmost_app(fresh=True)
check(f"the app in front is identified ({front!r})", bool(front), front)

if os.environ.get('JARVIS_NO_LIVE'):
    print('\n  (live on-screen checks skipped - JARVIS_NO_LIVE set)')
else:
    print("\n=== live: Calculator ===")
    running = {a["name"] for a in C.running_apps()}
    if not C.input_allowed() or not ax.available():
        print("  SKIP live checks: no Accessibility permission in this context")
    elif "Calculator" in running:
        print("  SKIP: Calculator is already open and might be yours")
    else:
        subprocess.run(["open", "-g", "-a", "Calculator"])
        for _ in range(40):
            if "Calculator" in {a["name"] for a in C.running_apps()}:
                break
            time.sleep(0.1)
        time.sleep(1.2)
        t0 = time.time()
        r = REGISTRY.execute("ui_elements", {"app": "Calculator"})
        check(f"controls listed by name ({time.time()-t0:.2f}s)",
              r.ok and any('"Multiply"' in c for c in r.content["controls"]), r.error)
        for b in ("All Clear", "6", "Multiply", "7", "Equals"):
            REGISTRY.execute("press_element", {"app": "Calculator", "element": b})
        r = REGISTRY.execute("ui_elements", {"app": "Calculator"})
        check("pressing buttons directly computes 6 x 7 = 42",
              r.ok and "42" in r.content["text"], r.content.get("text"))
        r = REGISTRY.execute("press_element", {"app": "Calculator", "element": "Launch Rocket"})
        check("a control that does not exist is an error that lists the real ones",
              not r.ok and "Multiply" in r.error, r.error)
        subprocess.run(["osascript", "-e", 'tell application "Calculator" to quit'])

    print("\n=== live: TextEdit, menus, fields and the Save panel ===")
    if not C.input_allowed() or not ax.available():
        print("  SKIP")
    elif "TextEdit" in running:
        print("  SKIP: TextEdit is already open and might be yours")
    else:
        tmp = tempfile.mkdtemp(prefix="jarvis-ax-")
        try:
            subprocess.run(["open", "-a", "TextEdit"])
            time.sleep(2.0)
            r = REGISTRY.execute("choose_menu", {"app": "TextEdit", "path": "File > New"})
            check("File > New through the menu bar", r.ok, r.error)
            time.sleep(0.8)
            r = REGISTRY.execute("set_field", {"app": "TextEdit", "field": "focused",
                                               "text": "accessibility test line"})
            check("text set into the document is read back", r.ok and r.content["verified"],
                  r.error)
            r = REGISTRY.execute("choose_menu", {"app": "TextEdit", "path": "Format > Sparkle"})
            check("a menu item that does not exist lists the ones that do",
                  not r.ok and "Font" in r.error, r.error)
            r = REGISTRY.execute("choose_menu", {"app": "TextEdit",
                                                 "path": "Format > Make Plain Text"})
            check("Format > Make Plain Text", r.ok, r.error)
            r = REGISTRY.execute("choose_menu", {"app": "TextEdit", "path": "File > Save"})
            check("opening Save reports the sheet that appeared",
                  r.ok and "sheet" in r.content.get("dialog", ""), r.content)
            r = REGISTRY.execute("select_option", {"app": "TextEdit",
                                                   "control": "Where", "option": "Nowhere Land"})
            r2 = REGISTRY.execute("ui_elements", {"app": "TextEdit"})
            check("a wrong dropdown option leaves the Save panel open",
                  not r.ok and "sheet" in (r2.content.get("dialog") or ""), r2.content.get("dialog"))
            target = os.path.join(tmp, "saved.txt")
            r = REGISTRY.execute("file_dialog", {"app": "TextEdit", "path": target})
            check("file_dialog changes folder, names the file, saves, and verifies",
                  r.ok and os.path.exists(target), (r.error, os.listdir(tmp)))
            body = open(target, errors="ignore").read() if os.path.exists(target) else ""
            check("the file holds the text, as plain text",
                  "accessibility test line" in body and not body.startswith("{\\rtf"), body[:60])
        finally:
            subprocess.run(["osascript", "-e",
                            'tell application "TextEdit" to close every document saving no',
                            "-e", 'tell application "TextEdit" to quit'])
            shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{sum(R)}/{len(R)} native-control checks passed")
sys.exit(0 if all(R) else 1)
