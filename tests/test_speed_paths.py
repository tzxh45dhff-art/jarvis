"""
Speed changes must not cost accuracy: instant answers stay narrow, every model
call in a turn offers the identical tool list (so the prompt cache holds), and
file tools confirm their own effect so the model does not re-check.
"""
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _isolate  # noqa: F401  (keeps tests out of the user's memory)

from jarvis.core import fastpath as F
from jarvis.core.agent import Agent
from jarvis.core.protocol import ToolCall
from jarvis.tools import REGISTRY, ToolResult

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {str(e)[:300]}"))
    R.append(bool(c))


print("=== instant answers are narrow ===")
for q in ("what time is it", "What's the time?", "jarvis, what time is it now",
          "what's the date", "what day is it", "what's my battery at", "battery",
          "how much battery do I have left"):
    check(f"answered instantly: {q!r}", F.answer(q) is not None, F.answer(q))
for q in ("what time is it in Tokyo", "is my battery healthy", "set a timer for 5 minutes",
          "what time does the store close", "what's the date of the next full moon",
          "open the battery settings", "what time is it in London and New York",
          "tell me the time and play some music"):
    check(f"left to the model: {q!r}", F.answer(q) is None, F.answer(q))


def D(kind, text="", tool_call=None):
    return types.SimpleNamespace(kind=kind, text=text, tool_call=tool_call,
                                 n_tokens=1, tps=1, prompt_tps=1)


class Recorder:
    """Records the tool list offered on every call."""
    _kind = "azure"

    def __init__(self, script):
        self.script, self.n, self.offered = script, 0, []

    def stream(self, messages, tools=None, think=False, max_tokens=None,
               temperature=None, stop_event=None):
        self.offered.append(tuple(t["function"]["name"] for t in (tools or [])))
        last = messages[-1].get("content") or ""
        if "Plan how to carry out" in last:
            yield D("text", "1. go"); yield D("done"); return
        if "Before answering" in last:
            # a misbehaving check that tries to act again
            yield D("tool_call", tool_call=ToolCall("browser_open", {"url": "https://x.y"}, "v1"))
            yield D("done"); return
        self.n += 1
        step = self.script(self.n)
        if step:
            yield D("tool_call", tool_call=ToolCall(*step, f"c{self.n}"))
        else:
            yield D("text", "Done.")
        yield D("done")


ran = []
real = (REGISTRY.execute, REGISTRY.execute_many)


def fake(name, args, **k):
    ran.append(name)
    return ToolResult(True, content={"ok": True}, display=name)


REGISTRY.execute = fake
REGISTRY.execute_many = lambda pairs: [fake(n, a) for n, a in pairs]

print("\n=== one tool list for the whole turn ===")
llm = Recorder(lambda n: {1: ("open_app", {"app": "Notes"}), 2: ("press_key", {"keys": "cmd+n"})}.get(n))
ag = Agent(llm)
ag._relevant_memories = lambda t: ""
ag._auto_capture = lambda a, b: None
ag.follow_ups = False
Agent._should_think = lambda self, t: True
for _ in ag.run("open notes and then make a new note and type hello and save it", max_steps=10):
    pass
check("several calls were made (plan, steps, check)", len(llm.offered) >= 3, len(llm.offered))
check("every call offered exactly the same tools (cache-safe)",
      len(set(llm.offered)) == 1, [len(o) for o in llm.offered])
check("the check could not reopen a page", "browser_open" not in ran, ran)
REGISTRY.execute, REGISTRY.execute_many = real

print("\n=== file tools confirm their own effect ===")
d = Path(tempfile.mkdtemp())
r = REGISTRY.execute("write_file", {"path": str(d / "a.txt"), "content": "hello"})
check("write_file reads back and says verified", r.ok and r.content["verified"] is True, r.content)
r = REGISTRY.execute("edit_file", {"path": str(d / "a.txt"), "old": "hello", "new": "bye"})
check("edit_file says verified", r.ok and r.content["verified"] is True, r.content)
r = REGISTRY.execute("make_dir", {"path": str(d / "sub")})
check("make_dir says verified", r.ok and r.content["verified"] is True, r.content)
r = REGISTRY.execute("copy_file", {"source": str(d / "a.txt"), "dest": str(d / "sub")})
check("copy_file says verified", r.ok and r.content["verified"] is True, r.content)
r = REGISTRY.execute("move_file", {"source": str(d / "a.txt"), "dest": str(d / "b.txt")})
check("move_file says verified", r.ok and r.content["verified"] is True, r.content)

print("\n=== model routing: cheap for simple, strong at the first sign of trouble ===")
from jarvis.core import router
for q in ("convert 5 miles to km", "who won the 2024 Nobel Prize in Physics", "define ephemeral",
          "what's my battery level?", "how long is the flight from Delhi to Dubai", "what time is it in Tokyo"):
    check(f"fast: {q!r}", router.pick(q) == "fast")
for q in ("open safari and log in to my bank", "solve the next leetcode problem",
          "create a file then email it to Asha", "play strawberry swing",
          "what's on my screen", "a" * 3 + " " + "word " * 20,
          "kal delhi ka weather kya hai", "आज का मौसम"):
    check(f"strong: {q[:40]!r}", router.pick(q) == "strong")

print("\n=== plain web lookups run on the cheap model ===")
for q in ("what's the price of gold in India today?", "who won the Formula 1 race last week",
          "weather in Delhi tomorrow", "latest version of Python", "RBI repo rate right now",
          "who is the CEO of Nvidia", "search for the iPhone 17 release date"):
    check(f"web lookup: {q!r}", router.pick_web(q))
for q in ("what's my battery level?", "open safari and search the price of gold", "play some music",
          "kal delhi ka weather kya hai", "what's the weather in Delhi and then remind me to carry an umbrella",
          "compare the iPhone 17 and the Galaxy S26 cameras", "write a report on solar panels",
          "what's 25 times 8", "send my mom the weather", "hello", "latest emails in my inbox",
          "what is my wifi password", "आज दिल्ली का मौसम क्या है"):
    check(f"not a web lookup: {q[:44]!r}", not router.pick_web(q))

import jarvis.core.azure as AZ
real_settings = AZ.settings
AZ.settings = lambda: {"fast_deployment": "gpt-6-luna"}


class Routed(Recorder):
    def __init__(self, script):
        super().__init__(script)
        self.turn_model = None
        self.models = []

    def stream(self, messages, **kw):
        self.models.append(self.turn_model or "strong")
        yield from super().stream(messages, **kw)


def fail_then_ok(name, args, **k):
    ran.append(name)
    if len(ran) == 1:
        return ToolResult(False, error="boom")
    return ToolResult(True, content={"ok": True}, display=name)


ran.clear()
REGISTRY.execute = fail_then_ok
REGISTRY.execute_many = lambda pairs: [fail_then_ok(n, a) for n, a in pairs]
llm = Routed(lambda n: {1: ("convert_units", {"value": 1, "from_unit": "mi", "to_unit": "km"}),
                        2: ("convert_units", {"value": 1, "from_unit": "mile", "to_unit": "km"})}.get(n))
ag = Agent(llm)
ag._relevant_memories = lambda t: ""
ag._auto_capture = lambda a, b: None
ag.follow_ups = False
statuses = [e.text for e in ag.run("convert 1 mile to km", max_steps=6) if e.type == "status"]
check("a simple request starts on the cheap model", llm.models[0] == "gpt-6-luna", llm.models)
check("after a failed step the strong model takes over", "strong" in llm.models[1:], llm.models)
check("and says so", any("stronger model" in s for s in statuses), statuses)
check("the turn model is reset afterwards", llm.turn_model is None)

# A web lookup uses the cheap model only when web_deployment is set, with no fast_deployment needed.
for cfg, want in (({}, "strong"), ({"web_deployment": ""}, "strong"), ({"web_deployment": "gpt-6-luna"}, "gpt-6-luna")):
    AZ.settings = lambda c=cfg: dict(c)
    ran.clear()
    REGISTRY.execute = fail_then_ok
    REGISTRY.execute_many = lambda pairs: [fail_then_ok(n, a) for n, a in pairs]
    llm = Routed(lambda n: {1: ("web_answer", {"question": "gold price"})}.get(n))
    ag = Agent(llm)
    ag._relevant_memories = lambda t: ""
    ag._auto_capture = lambda a, b: None
    ag.follow_ups = False
    list(ag.run("what's the price of gold in India today?", max_steps=6))
    check(f"web lookup with settings {cfg or '{}'} starts on {want}", llm.models[0] == want, llm.models)
    if want == "gpt-6-luna":
        check("...and hands over to the strong model when a step fails", "strong" in llm.models[1:], llm.models)
AZ.settings = lambda: {"fast_deployment": "gpt-6-luna"}

ran.clear()
REGISTRY.execute = fake
REGISTRY.execute_many = lambda pairs: [fake(n, a) for n, a in pairs]
llm = Routed(lambda n: {1: ("open_app", {"app": "Notes"})}.get(n))
ag = Agent(llm)
ag._relevant_memories = lambda t: ""
ag._auto_capture = lambda a, b: None
ag.follow_ups = False
list(ag.run("open notes and make a new note", max_steps=6))
check("an action request never touches the cheap model", "gpt-6-luna" not in llm.models, llm.models)
AZ.settings = real_settings
REGISTRY.execute, REGISTRY.execute_many = real

print(f"\n{sum(R)}/{len(R)} speed-path checks passed")
sys.exit(0 if all(R) else 1)
