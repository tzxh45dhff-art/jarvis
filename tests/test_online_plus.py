"""
YouTube, raw HTTP, directions and the scheduler. Offline logic is checked
first; network checks run only when JARVIS_NO_LIVE is unset.
"""
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.tools import REGISTRY
from jarvis.tools import online_plus as O

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {str(e)[:300]}"))
    R.append(bool(c))


def run(tool, **kw):
    return REGISTRY.execute(tool, kw)


print("=== captions ===")
VTT = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.000
hello and welcome

00:00:03.000 --> 00:00:05.000
hello and welcome
to the channel

00:00:05.000 --> 00:00:07.000
<c.colorE5E5E5>today we build</c> a thing
"""
t = O.parse_vtt(VTT)
check("scrolling repeats are collapsed", t.count("hello and welcome") == 1, t)
check("styling tags are stripped", "<c" not in t and "today we build a thing" in t, t)
check("timestamps are optional", O.parse_vtt(VTT, True).startswith("[00:00:01] hello"), O.parse_vtt(VTT, True))
check("duration formatting", O._fmt_duration(3725) == "1:02:05" and O._fmt_duration(65) == "1:05")

print("\n=== http_request guardrails ===")
v = REGISTRY.assess("http_request", {"url": "https://example.com", "method": "GET"})
check("a read runs without asking", not v.needs_confirm and v.allowed, v)
v = REGISTRY.assess("http_request", {"url": "https://example.com/api", "method": "POST"})
check("a POST asks first", v.needs_confirm, v)
v = REGISTRY.assess("http_request", {"url": "http://169.254.169.254/latest/meta-data/", "method": "GET"})
check("the cloud metadata address is refused", not v.allowed, v)
v = REGISTRY.assess("http_request", {"url": "http://192.168.1.1/admin", "method": "GET"})
check("a private LAN address is refused", not v.allowed, v)
v = REGISTRY.assess("http_request", {"url": "http://localhost:3000/health", "method": "GET"})
check("localhost is allowed (local dev servers)", v.allowed, v)

import ipaddress
check("a NAT64 address is judged by the IPv4 inside it (public stays public)",
      not O._effective_ip(ipaddress.ip_address("64:ff9b::12e9:b617")).is_private)
check("...and a NAT64-wrapped private address is still private",
      O._effective_ip(ipaddress.ip_address("64:ff9b::c0a8:0101")).is_private)
check("IPv4-mapped is unwrapped", str(O._effective_ip(ipaddress.ip_address("::ffff:10.0.0.1"))) == "10.0.0.1")

print("\n=== schedule parsing ===")
cases = {
    "every day at 8:30": dict(hour=8, minute=30, weekdays=None, recurring=True),
    "weekdays at 9am": dict(hour=9, minute=0, weekdays=[1, 2, 3, 4, 5], recurring=True),
    "every monday 7am": dict(hour=7, minute=0, weekdays=[1], recurring=True),
    "every sunday at 6pm": dict(hour=18, minute=0, weekdays=[0], recurring=True),
    "weekends at 10:15 pm": dict(hour=22, minute=15, weekdays=[0, 6], recurring=True),
    "12am every day": dict(hour=0, minute=0, weekdays=None, recurring=True),
}
for text, want in cases.items():
    got = O.parse_when(text) or {}
    check(f"{text!r}", all(got.get(k) == v for k, v in want.items()), got)
one = O.parse_when("tomorrow at 7am")
check("tomorrow is a one-off with a date", one and one["date"] and not one["recurring"], one)
check("a bare time is a one-off at its next occurrence", (O.parse_when("at 6pm") or {}).get("recurring") is False)
check("nonsense is refused", O.parse_when("whenever") is None)

print("\n=== launchd job ===")
pl = O.build_plist("abc123", 8, 30, [1, 2, 3, 4, 5])
check("the label is namespaced", pl["Label"] == "com.example.jarvis.task.abc123")
check("one calendar entry per weekday", len(pl["StartCalendarInterval"]) == 5)
check("it runs the runner module with the id", pl["ProgramArguments"][-2:] == ["jarvis.scheduled_runner", "abc123"])
check("it only runs in a logged-in session", pl["LimitLoadToSessionType"] == "Aqua")
check("it is a valid plist", plistlib.loads(plistlib.dumps(pl)) == pl)
v = REGISTRY.assess("schedule_task", {"action": "add", "when": "daily 8am", "prompt": "weather"})
check("creating a task asks first", v.needs_confirm)
check("listing does not", not REGISTRY.assess("schedule_task", {"action": "list"}).needs_confirm)

print("\n=== scheduled runner (stub engine) ===")
import jarvis.scheduled_runner as SR
from jarvis.config import DATA_DIR as _DATA_DIR
tmp = _DATA_DIR / "tasks"
tmp.mkdir(parents=True, exist_ok=True)
(tmp / "testrun.json").write_text(json.dumps(
    {"id": "testrun", "when": "x", "prompt": "what is the weather", "one_shot": False}))
seen = {}


class StubClient:
    def connect(self, timeout=0):
        seen["connected"] = True

    def close(self):
        pass

    def run(self, text, voice=False, fresh=False, on_confirm=None):
        seen["text"], seen["fresh"] = text, fresh
        seen["confirm_answer"] = on_confirm({"tool": "send_email"}) if on_confirm else None
        yield {"type": "status", "text": "thinking"}
        yield {"type": "done", "text": "Sunny, 27 degrees."}


notes = []
SR.notify = lambda title, body: notes.append((title, body))
res = SR.run_task("testrun", client_factory=StubClient)
check("the answer is returned and notified", res == "Sunny, 27 degrees." and notes and notes[0][1] == res, (res, notes))
check("the engine is told it is unattended", "unattended" in seen["text"] and "what is the weather" in seen["text"])
check("confirmations are declined", seen["confirm_answer"] == (False, False))
(tmp / "testrun.json").unlink(missing_ok=True)
check("it never wipes the user's conversation", seen["fresh"] is False)
check("the result is saved", "Sunny" in (tmp / "testrun.last.txt").read_text())
for f in ("testrun.json", "testrun.last.txt"):
    (tmp / f).unlink(missing_ok=True)

print("\n=== real launchd round trip ===")
REGISTRY.confirm_hook = lambda n, a, v: True            # the user says yes
r = run("schedule_task", action="add", when="every day at 3:33am", prompt="say what day it is")
if r.ok:
    tid = r.content["id"]
    check("a task is created", r.content["verified"])
    listed = run("schedule_task", action="list").content["tasks"]
    check("and listed", any(t["id"] == tid for t in listed))
    plist = Path.home() / "Library/LaunchAgents" / f"com.example.jarvis.task.{tid}.plist"
    check("its plist exists", plist.exists())
    ls = subprocess.run(["/bin/launchctl", "list", f"com.example.jarvis.task.{tid}"],
                        capture_output=True, text=True)
    check("launchd knows about it", ls.returncode == 0, ls.stderr)
    r2 = run("schedule_task", action="remove", id=tid)
    check("it is removed", r2.ok and not plist.exists())
    ls = subprocess.run(["/bin/launchctl", "list", f"com.example.jarvis.task.{tid}"],
                        capture_output=True, text=True)
    check("and launchd forgets it", ls.returncode != 0)
else:
    check("a task is created", False, r.error)
check("a task needs a prompt", not run("schedule_task", action="add", when="daily 8am", prompt="").ok)
check("a task needs a time", not run("schedule_task", action="add", when="whenever", prompt="do the thing now").ok)

if not os.environ.get("JARVIS_NO_LIVE"):
    print("\n=== live network ===")
    r = run("youtube_tool", action="search", query="3blue1brown neural networks")
    check("youtube search", r.ok and len(r.content["videos"]) >= 3, r.content if r.ok else r.error)
    r = run("youtube_tool", action="info", url="https://www.youtube.com/watch?v=aircAruvnKk")
    check("youtube info", r.ok and "neural" in (r.content.get("title") or "").lower(), r.content if r.ok else r.error)
    r = run("youtube_tool", action="transcript", url="https://www.youtube.com/watch?v=aircAruvnKk")
    check("youtube transcript", r.ok and r.content["total_chars"] > 2000, r.content if r.ok else r.error)
    r = run("http_request", url="https://httpbin.org/json")
    check("a JSON GET parses", r.ok and "json" in r.content, r.content if r.ok else r.error)
    r = run("directions", destination="Taj Mahal, Agra", origin="India Gate, New Delhi")
    check("directions: Delhi to Agra is about 200 km", r.ok and 150 < r.content["distance_km"] < 260,
          r.content if r.ok else r.error)

print(f"\n{sum(R)}/{len(R)} online-tool checks passed")
sys.exit(0 if all(R) else 1)
