"""
Brightness, dark mode, Wi-Fi: set exactly, read back.

Regression cover: "set brightness to max" once took the screen to 0, because
there was no brightness tool and the model improvised. Offline - the display
and system calls are stubbed, so nothing on screen changes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.safety.policy import needs_confirmation
from jarvis.tools import REGISTRY
from jarvis.tools import system_controls as S

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


print("=== what 'max' means ===")
for said, want in (("max", 100), ("MAX", 100), ("full", 100), ("100%", 100),
                   ("min", 0), ("up", 50), ("down", 30), ("+25", 65), ("-60", 0),
                   ("0.8", 80), ("70", 70), ("150", 100)):
    check(f"{said!r} from 40% -> {want}%", S.parse_level(said, 40) == want,
          S.parse_level(said, 40))
try:
    S.parse_level("as bright as possible please", 40)
    check("unreadable input is refused, never silently 0", False)
except ValueError:
    check("unreadable input is refused, never silently 0", True)


class FakeDS:
    level = 0.4
    ignore = False

    def DisplayServicesGetBrightness(self, d, ptr): ptr._obj.value = FakeDS.level; return 0
    def DisplayServicesSetBrightness(self, d, v):
        if not FakeDS.ignore:
            FakeDS.level = v.value
        return 0
    def DisplayServicesCanChangeBrightness(self, d): return True


real = (S._ds, S._displays)
S._ds, S._displays = (lambda: FakeDS()), (lambda: [1])
print("\n=== setting it ===")
r = REGISTRY.execute("set_brightness", {"level": "max"})
check("'max' sets 100% and reports what it read back",
      r.ok and r.content["brightness"] == 100 and r.content["before"] == 40, r.content)
FakeDS.ignore = True
r = REGISTRY.execute("set_brightness", {"level": "20"})
check("a display that did not change is a failure, not a success",
      not r.ok and "reports" in r.error, r)
FakeDS.ignore = False
S._displays = lambda: []
r = REGISTRY.execute("set_brightness", {"level": "max"})
check("no controllable display says so plainly", not r.ok and "external" in r.error)
S._ds, S._displays = real

print("\n=== safety ===")
check("brightness and dark mode need no confirmation",
      not needs_confirmation(REGISTRY.assess("set_brightness", {"level": "max"}))
      and not needs_confirmation(REGISTRY.assess("set_dark_mode", {"mode": "on"})))
check("turning Wi-Fi off asks first (it disconnects the cloud model)",
      needs_confirmation(REGISTRY.assess("set_wifi", {"on": False})))
check("turning it on does not", not needs_confirmation(REGISTRY.assess("set_wifi", {"on": True})))

print("\n=== the real display (read only) ===")
b = S.get_brightness()
check("brightness is readable on this Mac", b is None or 0 <= b <= 100, b)

print(f"\n{sum(R)}/{len(R)} system-control checks passed")
sys.exit(0 if all(R) else 1)
