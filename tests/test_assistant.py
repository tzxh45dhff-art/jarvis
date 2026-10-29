"""
Assistant-level tests: does JARVIS reach for the right context on its own?

These check tool *selection* and latency for the kinds of request a person
actually makes, not whether the model phrases the answer a particular way.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _isolate  # noqa: F401  (keeps tests out of the user's memory)

from jarvis.core.agent import Agent
from jarvis.core.llm import get_backend
from jarvis.tools import REGISTRY

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


print("=== startup ===")
b = get_backend()
t0 = time.time()
b.load()
t_load = time.time() - t0
t0 = time.time()
t_warm = b.warm()
print(f"  model load : {t_load:5.1f}s")
print(f"  cache warm : {t_warm:5.1f}s  ({len(b._cache_tokens)} tokens)")
check("warm start is fast (disk-cached prefix)", t_warm < 3.0, f"{t_warm:.1f}s")

asked = []


def confirm(name, args, verdict):
    asked.append((name, verdict.category))
    return False              # never actually send anything in a test


REGISTRY.confirm_hook = confirm
agent = Agent(b)
# warm kernels: the daemon does this at startup, so the suite should measure the
# steady state rather than first-pass Metal compilation
for _ in agent.run("Say ready.", max_steps=1):
    pass
agent.reset()
b.warm()


class Turn:
    def __init__(self, task):
        self.tools, self.text, self.meta = [], "", {}
        t0 = time.time()
        for ev in agent.run(task):
            if ev.type == "tool_start":
                self.tools.append(ev.tool)
            elif ev.type == "done":
                self.text, self.meta = ev.text, ev.meta
        self.elapsed = time.time() - t0

    def used(self, *names):
        return any(n in self.tools for n in names)


CASES = [
    # (request, acceptable tools, must_use_a_tool)
    ("Am I free tomorrow?",
     ("check_availability", "check_calendar", "check_reminders"), True),
    ("What's on my calendar this week?",
     ("check_calendar", "check_availability"), True),
    ("What do I need to do today?",
     ("check_reminders", "check_calendar", "check_availability"), True),
    ("What am I listening to?", ("now_playing", "system_status"), True),
    ("What's my battery at?", ("system_status",), True),
    ("What browser tabs do I have open?", ("browser_tabs",), True),
    ("What's in my clipboard?", ("read_clipboard",), True),
    ("What's the weather like?", ("get_weather", "web_search"), True),
    ("What is 25 times 8?", (), False),
]

print("\n=== does it fetch the right context? ===")
for task, expect, needs_tool in CASES:
    t = Turn(task)
    if needs_tool:
        ok = t.used(*expect)
        check(f"{task!r} -> {expect[0]}", ok,
              f"used {t.tools or 'no tools'}; said: {t.text[:110]}")
    else:
        check(f"{task!r} answers directly", not t.tools, t.tools)
    print(f"        {t.elapsed:5.2f}s  ttft={t.meta.get('ttft', 0):.2f}s  "
          f"tools={t.tools}")

print("\n=== composite: needs two sources in one answer ===")
t = Turn("Do I have anything scheduled tomorrow, and anything due? "
         "Check both the calendar and reminders.")
check("consults calendar and reminders",
      t.used("check_calendar", "check_availability") and t.used("check_reminders"),
      t.tools)
print(f"        {t.elapsed:5.2f}s  tools={t.tools}")

print("\n=== media control by voice-style phrasing ===")
t = Turn("Turn the volume down a bit.")
check("adjusts volume", t.used("set_volume"), t.tools)
print(f"        {t.elapsed:5.2f}s  tools={t.tools}")

print("\n=== sending is proposed, not performed ===")
asked.clear()
t = Turn("Send a text message to +10000000000 saying 'this is a JARVIS test'.")
check("attempted to send via send_message", t.used("send_message"), t.tools)
check("asked permission first", any(c == "send" for _, c in asked), asked)
print(f"        confirmations requested: {asked}")

print("\n=== latency summary ===")
ttfts = []
for q in ("Name a colour.", "Name another.", "One more."):
    t = Turn(q)
    ttfts.append(t.meta.get("ttft", 9))
print(f"  conversational ttft: {[round(x, 2) for x in ttfts]}")
check("conversational turns stay under 0.5s to first token",
      max(ttfts) < 0.5, ttfts)

print(f"\n{sum(R)}/{len(R)} assistant checks passed")
sys.exit(0 if all(R) else 1)
