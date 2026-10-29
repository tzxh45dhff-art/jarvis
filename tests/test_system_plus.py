"""Timers, processes, network, disk and windows. Real machine, no mutation of
anything the user owns (timers are cancelled, processes are ones we start)."""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.tools import REGISTRY
from jarvis.tools import system_plus as S

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {str(e)[:300]}"))
    R.append(bool(c))


def run(tool, **kw):
    return REGISTRY.execute(tool, kw)


print("=== timers ===")
for text, want in (("90", 90), ("5 min", 300), ("1h30m", 5400), ("1 hour 15 minutes", 4500),
                   ("45 seconds", 45), ("1:30:00", 5400), ("2:15", 135)):
    check(f"parse {text!r} -> {want}s", S.parse_duration(text) == want, S.parse_duration(text))
check("nonsense is not a duration", S.parse_duration("soonish") is None)
r = run("timer", action="set", duration="2 minutes", label="tea")
check("a timer is set and its process is alive", r.ok and r.content["verified"] and r.content["seconds"] == 120, r.content if r.ok else r.error)
tid = r.content["id"] if r.ok else ""
r = run("timer", action="list")
check("it shows in the list", r.ok and any(t["id"] == tid for t in r.content["timers"]), r.content if r.ok else r.error)
r = run("timer", action="cancel", id=tid)
check("it can be cancelled", r.ok and tid in r.content["cancelled"], r.content if r.ok else r.error)
time.sleep(0.3)
check("and is gone", not any(t["id"] == tid for t in run("timer", action="list").content["timers"]))
check("no duration is refused", not run("timer", action="set").ok)
check("over a week is refused", not run("timer", action="set", duration="800 hours").ok)
r = run("timer", action="set", at="11:59pm", label="late")
check("a clock time works", r.ok and 0 < r.content["seconds"] <= 86400, r.content if r.ok else r.error)
run("timer", action="cancel", id="all")

print("\n=== processes ===")
r = run("processes", action="top", limit=5)
check("top: five busiest", r.ok and len(r.content["processes"]) == 5, r.content if r.ok else r.error)
r = run("processes", action="top", sort="memory", limit=3)
check("top by memory is ordered", r.ok and r.content["processes"][0]["memory_mb"] >= r.content["processes"][-1]["memory_mb"])
r = run("processes", action="find", name="python")
check("find by name", r.ok and r.content["processes"], r.content if r.ok else r.error)
v = REGISTRY.assess("processes", {"action": "quit", "name": "x"})
check("quitting always asks first", v.needs_confirm)
check("looking never asks", not REGISTRY.assess("processes", {"action": "top"}).needs_confirm)
REGISTRY.confirm_hook = lambda n, a, v: True
check("a system-critical process is refused", not run("processes", action="quit", name="WindowServer").ok)
check("pid 1 is refused", not run("processes", action="quit", pid=1).ok)
p = subprocess.Popen(["/bin/sleep", "300"])
time.sleep(0.2)
r = run("processes", action="quit", pid=p.pid)
check("a process of ours is ended and verified", r.ok and r.content["ended"], r.content if r.ok else r.error)
p.wait(timeout=3)

print("\n=== network ===")
r = run("network_info", action="ping", host="1.1.1.1")
check("ping answers with a time", r.ok and r.content["reachable"] and r.content["avg_ms"] is not None, r.content if r.ok else r.error)
r = run("network_info", action="dns", host="example.com")
check("dns resolves", r.ok and r.content["addresses"], r.content if r.ok else r.error)
r = run("network_info", action="site", url="https://example.com")
check("site check: up with a status", r.ok and r.content["up"] and r.content["status"] == 200, r.content if r.ok else r.error)
r = run("network_info", action="site", url="https://this-domain-should-not-exist-jarvis.invalid")
check("an unreachable site is reported, not crashed", r.ok and r.content["up"] is False, r.content if r.ok else r.error)
r = run("network_info", action="status")
check("status: local ip and internet", r.ok and r.content.get("internet"), r.content if r.ok else r.error)

print("\n=== disk ===")
r = run("disk_usage", path="~/Downloads/fuck/jarvis/jarvis")
check("biggest children of a folder", r.ok and r.content["biggest"] and r.content["startup_disk"]["free_gb"] > 0, r.content if r.ok else r.error)
r = run("disk_usage", path="~/Downloads/fuck/jarvis/jarvis", min_mb=0)
check("a missing path is an error", not run("disk_usage", path="/definitely/not/here").ok)

print("\n=== windows ===")
r = run("window_manage", action="list")
check("list: windows and screens", r.ok and r.content["screens"], r.content if r.ok else r.error)
scr = {"x": 0, "y": 38, "w": 1512, "h": 900}
check("left half", S.tile_rect(scr, "left") == (0, 38, 756, 900))
check("right half", S.tile_rect(scr, "right") == (756, 38, 756, 900))
check("top-right quarter", S.tile_rect(scr, "top_right") == (756, 38, 756, 450))
check("maximize fills the usable area", S.tile_rect(scr, "maximize") == (0, 38, 1512, 900))
check("thirds add up", abs(S.tile_rect(scr, "left_third")[2] * 3 - 1512) < 1e-6)
check("unknown placement is None", S.tile_rect(scr, "diagonal") is None)

print(f"\n{sum(R)}/{len(R)} system checks passed")
sys.exit(0 if all(R) else 1)
