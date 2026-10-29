"""
App-integration tests: Music, Calendar, Reminders, Contacts, system state.

Must be run from a terminal that has been granted Automation, Calendars,
Reminders and Contacts access — those are per-app macOS permissions and a
process without them cannot speak for the user.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.safety.policy import needs_confirmation
from jarvis.tools import REGISTRY

R = []
TIMING = {}


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))
    return bool(c)


def call(name, args=None, label=None):
    t0 = time.time()
    r = REGISTRY.execute(name, args or {})
    TIMING[label or name] = time.time() - t0
    return r


print("=== contacts ===")
r = call("find_contact", {"name": "a", "limit": 3})
check("contacts readable", r.ok, r.error)
if r.ok:
    n = len(r.content.get("matches", []))
    check("returns matches", n > 0, r.content)
    check(f"contact lookup is fast ({TIMING['find_contact']:.2f}s)",
          TIMING["find_contact"] < 2.0, TIMING["find_contact"])
    t0 = time.time()
    call("find_contact", {"name": "b", "limit": 3}, "find_contact_cached")
    check(f"second lookup uses the cache ({TIMING['find_contact_cached']:.3f}s)",
          TIMING["find_contact_cached"] < 0.2, TIMING["find_contact_cached"])

print("\n=== calendar ===")
r = call("check_calendar", {"when": "this week"})
check("calendar readable", r.ok, r.error)
if r.ok:
    check(f"calendar query is fast ({TIMING['check_calendar']:.3f}s)",
          TIMING["check_calendar"] < 1.5, TIMING["check_calendar"])
    print(f"        {r.content['count']} event(s) this week")
    for e in r.content["events"][:4]:
        print(f"          - {e['title']} @ {e['start']}")

r = call("check_availability", {"when": "tomorrow"})
check("availability computed", r.ok, r.error)
if r.ok:
    c = r.content
    check("returns busy blocks and free slots",
          "busy" in c and "free_slots" in c, list(c))
    print(f"        {c['date']}: busy={len(c['busy'])} "
          f"free_slots={len(c['free_slots'])} clear={c['is_clear']}")

print("\n=== reminders ===")
r = call("check_reminders", {})
check("reminders readable", r.ok, r.error)
if r.ok:
    print(f"        {r.content['count']} open, "
          f"{r.content['overdue_count']} overdue")

print("\n=== reminders: create then complete (round trip) ===")
marker = f"JARVIS selftest {int(time.time())}"
r = call("create_reminder", {"title": marker, "due": "2030-01-01T09:00"})
check("reminder created", r.ok, r.error)
if r.ok:
    r2 = call("check_reminders", {})
    found = any(marker in x["title"] for x in r2.content.get("reminders", []))
    check("created reminder is readable back", found)
    r3 = call("complete_reminder", {"title": marker})
    check("reminder completed (cleanup)", r3.ok, r3.error)

print("\n=== music ===")
r = call("now_playing")
check("now_playing works", r.ok, r.error)
print(f"        {r.display}")
r = call("search_music", {"query": "a", "limit": 3})
check("library search works", r.ok, r.error)
# The first Apple Event to Music launches it if it was not running, which costs
# a few seconds once. now_playing deliberately does not wake it just to answer
# "nothing is playing", so that cost lands here. Measure the steady state.
r = call("search_music", {"query": "a", "limit": 3}, "search_music_warm")
check(f"library search is fast once Music is up "
      f"({TIMING['search_music_warm']:.2f}s)",
      TIMING["search_music_warm"] < 1.5, TIMING["search_music_warm"])

print("\n=== system state ===")
r = call("system_status")
check("system status readable", r.ok, r.error)
check(f"status is fast ({TIMING['system_status']:.2f}s)",
      TIMING["system_status"] < 1.0, TIMING["system_status"])
if r.ok:
    keys = set(r.content)
    check("reports battery and disk", "battery" in keys and "disk" in keys, keys)

r = call("read_clipboard")
check("clipboard readable", r.ok, r.error)

r = call("list_shortcuts")
check("shortcuts listed", r.ok, r.error)
if r.ok:
    print(f"        {r.content['count']} shortcut(s)")

print("\n=== safety: sending is always gated ===")
asked = []


def refuse(name, args, verdict):
    asked.append((name, verdict.category, verdict.reason))
    return False            # decline, so nothing is actually sent


REGISTRY.confirm_hook = refuse
r = call("send_message", {"to": "+10000000000", "text": "selftest — not sent",
                          "service": "imessage"})
check("send_message asked for confirmation",
      any(c == "send" for _, c, _ in asked), asked)
check("declining means nothing was sent",
      not r.ok and "Declined" in (r.error or ""), r.error)

asked.clear()
r = call("send_email", {"to": "nobody@example.invalid", "subject": "selftest",
                        "body": "not sent"})
check("send_email asked for confirmation",
      any(c == "send" for _, c, _ in asked), asked)

asked.clear()
r = call("run_shortcut", {"name": "Nonexistent Shortcut"})
check("run_shortcut asked for confirmation",
      any(c == "shortcut" for _, c, _ in asked), asked)

print("\n=== safety: routine control is NOT gated ===")
asked.clear()
r = call("screen_info")
check("read-only tools never prompt", not asked and r.ok, asked)

REGISTRY.confirm_hook = None

print("\n--- timings ---")
for k, v in sorted(TIMING.items(), key=lambda x: -x[1]):
    print(f"  {k:<24} {v:6.3f}s")

print(f"\n{sum(R)}/{len(R)} integration-app checks passed")
sys.exit(0 if all(R) else 1)
