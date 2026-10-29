"""
Autonomy and interaction safeguards.

These cover the failure modes that made JARVIS unusable in real sessions:
a confirmation prompt that could not be answered, and a model that reissued the
same failing tool call until the step budget ran out.

No model is loaded — the agent is driven with a stub LLM so the loop logic is
tested directly and the suite runs in under a second.
"""
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _isolate  # noqa: F401  (keeps tests out of the user's memory)

from jarvis.core.agent import Agent
from jarvis.core.protocol import ToolCall
from jarvis.safety.policy import needs_confirmation
from jarvis.tools import REGISTRY, control

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


# --------------------------------------------------------------- loop breaker
class RepeatingLLM:
    """Reissues one doomed call forever, as the real model did."""

    def __init__(self, tool="run_shell", args=None):
        self.tool = tool
        self.args = args or {"command": "ls /nope | grep x"}
        self.n = 0

    def stream(self, messages, tools=None, think=False, max_tokens=None,
               temperature=None, stop_event=None):
        self.n += 1
        if tools is None:                     # the "stop and explain" prompt
            yield types.SimpleNamespace(kind="text", text="I'm blocked.",
                                        tool_call=None, n_tokens=1, tps=1,
                                        prompt_tps=1)
            yield types.SimpleNamespace(kind="done", text="", tool_call=None,
                                        n_tokens=1, tps=1, prompt_tps=1)
            return
        yield types.SimpleNamespace(
            kind="tool_call", text="",
            tool_call=ToolCall(name=self.tool, arguments=self.args,
                               id=f"c{self.n}"),
            n_tokens=1, tps=1, prompt_tps=1)
        yield types.SimpleNamespace(kind="done", text="", tool_call=None,
                                    n_tokens=1, tps=1, prompt_tps=1)


def drive(llm, task="do the thing", max_steps=40):
    ag = Agent(llm)
    ag._relevant_memories = lambda t: ""
    ag._auto_capture = lambda a, b: None
    out = {"tools": 0, "short": 0, "meta": {}, "text": ""}
    t0 = time.time()
    for ev in ag.run(task, max_steps=max_steps):
        if ev.type == "tool_start":
            out["tools"] += 1
        elif ev.type == "tool_end" and "repeat of a call" in (ev.text or ""):
            out["short"] += 1
        elif ev.type == "done":
            out["meta"], out["text"] = ev.meta, ev.text
    out["elapsed"] = time.time() - t0
    return out


print("=== a repeated failing call must not burn the step budget ===")
o = drive(RepeatingLLM())
check(f"stops early ({o['meta'].get('steps')} steps, budget 40)",
      o["meta"].get("steps", 99) <= 8, o["meta"])
check("short-circuits the repeats instead of running them",
      o["short"] >= 2, o)
check("still produces an answer for the user", bool(o["text"]), o["text"])
check(f"and does it quickly ({o['elapsed']:.2f}s)", o["elapsed"] < 5, o["elapsed"])

print("\n=== a failing inspect_ui must not loop either ===")
o2 = drive(RepeatingLLM("inspect_ui", {"app": "NoSuchApp12345"}))
check(f"stops early ({o2['meta'].get('steps')} steps)",
      o2["meta"].get("steps", 99) <= 8, o2["meta"])

print("\n=== distinct calls are NOT short-circuited ===")


class VaryingLLM(RepeatingLLM):
    def stream(self, messages, tools=None, think=False, max_tokens=None,
               temperature=None, stop_event=None):
        self.n += 1
        if tools is None or self.n > 3:
            yield types.SimpleNamespace(kind="text", text="done.", tool_call=None,
                                        n_tokens=1, tps=1, prompt_tps=1)
            yield types.SimpleNamespace(kind="done", text="", tool_call=None,
                                        n_tokens=1, tps=1, prompt_tps=1)
            return
        yield types.SimpleNamespace(
            kind="tool_call", text="",
            tool_call=ToolCall(name="run_shell",
                               arguments={"command": f"echo attempt{self.n}"},
                               id=f"c{self.n}"),
            n_tokens=1, tps=1, prompt_tps=1)
        yield types.SimpleNamespace(kind="done", text="", tool_call=None,
                                    n_tokens=1, tps=1, prompt_tps=1)


o3 = drive(VaryingLLM())
check("different arguments still run", o3["short"] == 0, o3)
check("genuine retries are allowed", o3["tools"] >= 3, o3)

print("\n=== an observer tool that keeps returning the same thing stops ===")


class ObserverLoop(RepeatingLLM):
    """browser_read called forever, same page every time — the real failure."""


for tool, args, label in [("read_clipboard", {}, "read_clipboard"),
                          ("screen_info", {}, "screen_info")]:
    o = drive(ObserverLoop(tool, args))
    check(f"{label} loop stops ({o['meta'].get('steps')} steps, budget 40)",
          o["meta"].get("steps", 99) <= 6, o["meta"])

print("\n=== a turn's loop state does not poison the next turn ===")


class OneLoopThenNormal:
    def __init__(self):
        self.n = 0
        self.turn = 1

    def stream(self, messages, tools=None, think=False, max_tokens=None,
               temperature=None, stop_event=None):
        self.n += 1
        looping = self.turn == 1
        if tools is None or not looping:
            yield types.SimpleNamespace(kind="text", text="ok", tool_call=None,
                                        n_tokens=1, tps=1, prompt_tps=1)
            yield types.SimpleNamespace(kind="done", text="", tool_call=None,
                                        n_tokens=1, tps=1, prompt_tps=1)
            return
        yield types.SimpleNamespace(
            kind="tool_call", text="",
            tool_call=ToolCall(name="read_clipboard", arguments={},
                               id=f"c{self.n}"),
            n_tokens=1, tps=1, prompt_tps=1)
        yield types.SimpleNamespace(kind="done", text="", tool_call=None,
                                    n_tokens=1, tps=1, prompt_tps=1)


llm = OneLoopThenNormal()
ag2 = Agent(llm)
ag2._relevant_memories = lambda t: ""
ag2._auto_capture = lambda a, b: None
for _ in ag2.run("first", max_steps=40):
    pass
llm.turn, llm.n = 2, 0
blocked = 0
for ev in ag2.run("second", max_steps=40):
    if ev.type == "tool_end" and "already answered" in (ev.text or ""):
        blocked += 1
check("turn 2 is not blocked by turn 1's loop state", blocked == 0, blocked)

# ------------------------------------------------------- confirmation parsing
print("\n=== the confirmation prompt understands what people type ===")
from jarvis.ui import console as ui

for word in ("y", "Y", "yes", "ok", "sure", "allow"):
    check(f"{word!r} means yes", word.lower() in ui.YES)
for word in ("a", "always", "all"):
    check(f"{word!r} means always", word.lower() in ui.ALWAYS)
for word in ("n", "no", "cancel", "stop"):
    check(f"{word!r} means no", word.lower() in ui.NO)
check("yes and no do not overlap", not (ui.YES & ui.NO))
check("suspend_live exists so the spinner cannot eat the answer",
      callable(getattr(ui, "suspend_live", None)))

# --------------------------------------------------------------- permissions
print("\n=== routine control is not gated; sending still is ===")
cases = [
    ("TextEdit", {"text": "hello"}, False, "typing in a normal app"),
    ("TextEdit", {"keys": "return"}, False, "Return in a normal app"),
    ("TextEdit", {"keys": "cmd+s"}, False, "save in a normal app"),
    ("Messages", {"text": "draft"}, False, "typing a draft in Messages"),
    ("Messages", {"keys": "cmd+s"}, False, "non-sending key in Messages"),
    ("Messages", {"keys": "return"}, True, "Return in Messages (sends)"),
    ("WhatsApp", {"keys": "enter"}, True, "Enter in WhatsApp (sends"),
]
for app, args, want, label in cases:
    control._FRONT_CACHE.update(name=app, at=time.time())
    got = needs_confirmation(control._control_verdict(args))
    check(f"{label} -> confirm={want}", got == want, f"got {got}")

print("\n=== irreversible tools remain gated ===")
for tool, args in [("send_message", {"to": "x", "text": "y"}),
                   ("send_email", {"to": "a@b.c", "subject": "s", "body": "b"}),
                   ("trash_files", {"paths": ["/tmp/x"]}),
                   ("run_shortcut", {"name": "x"})]:
    v = REGISTRY.assess(tool, args)
    check(f"{tool} asks first", needs_confirmation(v), v)

print("\n=== the pointer is visible, not teleporting ===")
check("cursor glide is enabled by default", control.CURSOR_GLIDE > 0,
      control.CURSOR_GLIDE)
check("and can be disabled for speed",
      "JARVIS_CURSOR" in open(
          os.path.join(os.path.dirname(__file__), "..", "jarvis", "tools",
                       "control.py")).read())


print("\n=== natural conversation: follow-ups are rare, never in a row, never mid-job ===")
from jarvis.core.agent import Agent as _A
_ag = _A.__new__(_A)
_ag.follow_ups = True
_ag._turn_index = 10
_ag._last_followup_turn = -99
import random as _r
_r.seed(3)
asked = sum(_ag._wants_followup("how is your day going") for _ in range(400))
check("on light chat roughly half of replies are eligible, not all", 120 < asked < 280, asked)
check("a job request never gets one", not any(_ag._wants_followup("create a file called notes on my desktop") for _ in range(50)))
check("a problem report never gets one", not any(_ag._wants_followup("it keeps crashing and I'm in a hurry") for _ in range(50)))
_ag._last_followup_turn = 9
check("not on the turn after one was asked", not any(_ag._wants_followup("hello there") for _ in range(50)))
_ag._turn_index = 14
check("after a gap of a few turns it is allowed again", any(_ag._wants_followup("hello there") for _ in range(50)))
_ag.follow_ups = False
check("and it can be switched off", not any(_ag._wants_followup("hello there") for _ in range(50)))
from jarvis.core import prompts as _P
check("the style guide forbids interrogating", "Never interrogate" in _P.system_prompt())
check("and help-desk phrases", "Certainly!" in _P.system_prompt())

print(f"\n{sum(R)}/{len(R)} autonomy checks passed")
sys.exit(0 if all(R) else 1)