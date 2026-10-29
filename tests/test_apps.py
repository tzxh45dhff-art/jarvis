"""
Quitting apps: planning, confirmation, verification, and the shell side door.

Regression cover for a real failure: asked to "close everything except Claude
and Terminal", JARVIS ran a bulk AppleScript through run_shell. It skipped the
confirmation that quitting needs, every quit failed silently inside `try`, the
script exited 0, and JARVIS reported "Closed everything" with every app open.

Mostly offline (fake app lists and fake NSRunningApplication). One live check
quits a Calculator this test launches itself - skipped if Calculator was
already open, so it can never close something of yours.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AppKit

from jarvis.safety.policy import classify_shell, needs_confirmation
from jarvis.tools import REGISTRY
from jarvis.tools import control as C

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


FAKE = [
    {"name": "Finder", "pid": 101, "bundle_id": "com.apple.finder",
     "path": "/System/Library/CoreServices/Finder.app"},
    {"name": "Claude", "pid": 102, "bundle_id": "com.anthropic.claudefordesktop",
     "path": "/Applications/Claude.app"},
    {"name": "Terminal", "pid": 103, "bundle_id": "com.apple.Terminal",
     "path": "/System/Applications/Utilities/Terminal.app"},
    {"name": "Google Chrome", "pid": 104, "bundle_id": "com.google.Chrome",
     "path": "/Applications/Google Chrome.app"},
    {"name": "Notes", "pid": 105, "bundle_id": "com.apple.Notes",
     "path": "/System/Applications/Notes.app"},
    {"name": "WhatsApp", "pid": 106, "bundle_id": "net.whatsapp.WhatsApp",
     "path": "/Applications/WhatsApp.app"},
    {"name": "Antigravity IDE", "pid": 107, "bundle_id": "com.google.antigravity",
     "path": "/Applications/Antigravity IDE.app"},
    {"name": "Notes Helper Pro", "pid": 108, "bundle_id": "x.notespro",
     "path": "/Applications/Notes Helper Pro.app"},
]
real_running = C.running_apps
C.running_apps = lambda: [dict(a) for a in FAKE]

print("=== names ===")
check("invisible marks, case and .app are ignored",
      C._norm("‎WhatsApp.app") == "whatsapp")
found, _ = C._match("chrome", FAKE)
check("'chrome' means Google Chrome", [a["name"] for a in found] == ["Google Chrome"])
found, _ = C._match("antigravity", FAKE)
check("a unique partial name resolves", [a["name"] for a in found] == ["Antigravity IDE"])
found, _ = C._match("notes", FAKE)
check("an exact name beats a longer one containing it",
      [a["name"] for a in found] == ["Notes"], found)
found, why = C._match("no", FAKE)
check("an ambiguous name is refused, not guessed", not found and "several" in why, why)
found, why = C._match("Spotify", FAKE)
check("an app that is not running is reported as such", not found and not why)

print("\n=== 'close everything except Claude and Terminal' ===")
plan = C._plan_quit(None, ["Claude", "Terminal"])
targets = {t["name"] for t in plan["targets"]}
check("quits everything else", targets == {"Google Chrome", "Notes", "WhatsApp",
                                           "Antigravity IDE", "Notes Helper Pro"},
      targets)
check("keeps what was asked, and Finder",
      set(plan["kept"]) == {"Finder", "Claude", "Terminal"}, plan["kept"])
plan = C._plan_quit(None, [])
check("a sweep never takes down the terminal or Claude, even unasked",
      {"Claude", "Terminal", "Finder"} <= set(plan["kept"]), plan["kept"])
plan = C._plan_quit(["Terminal"], None)
check("but naming one explicitly is honoured",
      [t["name"] for t in plan["targets"]] == ["Terminal"])

print("\n=== confirmation ===")
v = REGISTRY.assess("quit_apps", {"all_except": ["Claude", "Terminal"]})
check("asks before quitting", needs_confirmation(v), v)
check("and the prompt names every app it will close",
      all(n in v.reason for n in ("Google Chrome", "Notes", "WhatsApp")), v.reason)
v = REGISTRY.assess("quit_apps", {"names": ["Spotify"]})
check("nothing to quit means nothing to confirm", not needs_confirmation(v), v)
v = REGISTRY.assess("quit_apps", {"names": ["Notes"], "force": True})
check("force-quit warns that unsaved work is lost", "lost" in v.reason, v.reason)
v = REGISTRY.assess("quit_app", {"name": "Notes"})
check("the single-app tool asks too", needs_confirmation(v), v)

print("\n=== quitting is verified, not assumed ===")


class FakeApp:
    calls = []

    def __init__(self, pid):
        self.pid = pid

    @classmethod
    def runningApplicationWithProcessIdentifier_(cls, pid):
        return cls(pid)

    def terminate(self):
        FakeApp.calls.append(("terminate", self.pid))
        return True

    def forceTerminate(self):
        FakeApp.calls.append(("force", self.pid))
        return True


real_appkit = AppKit.NSRunningApplication
AppKit.NSRunningApplication = FakeApp
real_alive, real_wait = C._pid_alive, C._QUIT_WAIT
C._QUIT_WAIT = 0.3

C._pid_alive = lambda pid: False                    # everything closes
r = C.quit_apps(names=["notes", "chrome"])
check("apps that closed are reported as quit",
      r.ok and set(r.content["quit"]) == {"Notes", "Google Chrome"}, r.content)
check("and the quit was graceful",
      all(k == "terminate" for k, _ in FakeApp.calls), FakeApp.calls)

FakeApp.calls.clear()
C._pid_alive = lambda pid: pid == 105               # Notes has a save dialog
r = C.quit_apps(all_except=["Claude", "Terminal"])
check("an app that is still running is NOT reported as quit",
      not r.ok and r.content["still_open"] == ["Notes"], r.content)
check("the others still count as quit",
      "WhatsApp" in r.content["quit"] and "Notes" not in r.content["quit"])
check("the error says why and warns against force-quitting",
      "save dialog" in r.error and "force" in r.error, r.error)

FakeApp.calls.clear()
C._pid_alive = lambda pid: False
r = C.quit_apps(names=["Notes"], force=True)
check("force only when asked, and then it force-quits",
      FakeApp.calls == [("force", 105)], FakeApp.calls)
r = C.quit_apps(names=["Spotify"])
check("asking to quit an app that isn't running is fine, and says so",
      r.ok and r.content["not_running"] == ["Spotify"], r.content)
r = C.quit_apps()
check("no target at all is an error, not a sweep", not r.ok)
r = C.quit_apps(names=["no"])
check("an ambiguous name quits nothing", not r.ok and "several" in r.error, r.error)

AppKit.NSRunningApplication = real_appkit
C._pid_alive, C._QUIT_WAIT = real_alive, real_wait

print("\n=== the shell side door is closed ===")
bulk = ("osascript -e 'tell application \"System Events\"\nset apps to name of "
        "every application process\nend tell\nrepeat with a in apps\ntry\ntell "
        "application a to quit\nend try\nend repeat'")
v = classify_shell(bulk)
check("a bulk AppleScript quit now needs confirmation", needs_confirmation(v), v)
v = classify_shell("osascript -e 'tell application \"System Events\" to get name "
                   "of first process whose frontmost is true'")
check("read-only AppleScript is still not gated", not needs_confirmation(v), v)
check("the model is told to prefer quit_apps",
      "not run_shell" in REGISTRY.get("quit_apps").description)

if os.environ.get('JARVIS_NO_LIVE'):
    print('\n  (live on-screen checks skipped - JARVIS_NO_LIVE set)')
else:
    print("\n=== the app list is fresh, not a frozen snapshot ===")
    C.running_apps = real_running
    live = C.running_apps()
    check("real running apps are listed", any(a["name"] == "Finder" for a in live),
          [a["name"] for a in live])
    check("each has a live pid", all(C._pid_alive(a["pid"]) for a in live))
    had_calc = any(a["name"] == "Calculator" for a in live)
    if had_calc:
        print("  SKIP live quit: Calculator is already open and might be yours")
    else:
        subprocess.run(["open", "-g", "-j", "-a", "Calculator"])
        for _ in range(50):
            if any(a["name"] == "Calculator" for a in C.running_apps()):
                break
            time.sleep(0.1)
        check("an app launched after startup is seen immediately",
              any(a["name"] == "Calculator" for a in C.running_apps()))
        time.sleep(1.5)                 # let it finish launching
        r = C.quit_app("calculator")
        gone = not any(a["name"] == "Calculator" for a in C.running_apps())
        check("a real app quits, and the result is verified", r.ok and gone,
              (r.ok, r.content, r.error, gone))
        if not gone:
            subprocess.run(["pkill", "-x", "Calculator"])

print(f"\n{sum(R)}/{len(R)} app-control checks passed")
sys.exit(0 if all(R) else 1)
