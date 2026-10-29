"""
"Done" must mean done where the user asked, and web tools must work anyway.

Regression cover for a real failure: asked "could u open leet code and solve and
run a problem for me", JARVIS opened LeetCode, web-searched a solution, ran it
in its own Python, wrote it to a file on the Desktop, and answered "I have
solved LeetCode 3". Asked "u sure?", it said it again. Nothing was entered or
submitted on LeetCode.

Offline: the model, the browser and the clipboard are all stubbed.
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _isolate  # noqa: F401  (keeps tests out of the user's memory)

from jarvis.core import agent as A
from jarvis.core.agent import Agent
from jarvis.core.protocol import ToolCall
from jarvis.tools import REGISTRY

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


def D(kind, text="", tool_call=None):
    return types.SimpleNamespace(kind=kind, text=text, tool_call=tool_call,
                                 n_tokens=1, tps=1, prompt_tps=1)


class ScriptedLLM:
    """Plays back what the real model did, then answers the check."""

    def __init__(self, verdict):
        self.verdict, self.n, self.saw_trace = verdict, 0, ""

    def stream(self, messages, tools=None, think=False, max_tokens=None,
               temperature=None, stop_event=None):
        last = messages[-1].get("content") or ""
        if "Plan how to carry out" in last:     # planning call: no tools run
            yield D("text", "1. open leetcode\n2. solve it\n3. submit")
            yield D("done")
            return
        if messages[-1]["role"] == "user" and "Before answering" in last:
            self.saw_trace = last
            yield D("text", self.verdict)
            yield D("done")
            return
        self.n += 1
        plan = {1: ("web_search", {"query": "leetcode 3 solution"}),
                2: ("write_file", {"path": "/tmp/jarvis-honesty-test.md",
                                   "content": "def f(): pass"})}
        if self.n in plan:
            name, args = plan[self.n]
            yield D("tool_call", tool_call=ToolCall(name, args, f"c{self.n}"))
        else:
            yield D("text", "I have solved LeetCode 3 and tested it.")
        yield D("done")


real_exec = REGISTRY.execute_many
REGISTRY.execute_many = lambda pairs: [
    types.SimpleNamespace(ok=True, error=None, display=f"{n} ok",
                          for_model=lambda: {"ok": True}) for n, _ in pairs]
real_exec1 = REGISTRY.execute
REGISTRY.execute = lambda n, a, **k: types.SimpleNamespace(
    ok=True, error=None, display=f"{n} ok", for_model=lambda: {"ok": True})


def run(llm, task):
    ag = Agent(llm)
    ag._relevant_memories = lambda t: ""
    ag._auto_capture = lambda a, b: None
    streamed, final = [], ""
    for ev in ag.run(task, max_steps=8):
        if ev.type == "text":
            streamed.append(ev.text)
        elif ev.type == "done":
            final = ev.text
    return "".join(streamed), final


TASK = "could u open leet code and solve and run a problem for me"
print("=== the exact request that failed ===")
check("it is recognised as a request to do something",
      bool(A._ACTION.search(TASK)))
for q in ("what's my battery at?", "hi", "what is 25 times 8?"):
    check(f"{q!r} is a question, not a job", not (A._ACTION.search(q) and
                                                  Agent._needs_tools(q)))

llm = ScriptedLLM("I did not solve anything on LeetCode. I wrote a solution to a "
                  "file and tested it locally; nothing was entered or submitted "
                  "on LeetCode.")
streamed, final = run(llm, TASK)
check("the checker sees the tools that really ran",
      "web_search" in llm.saw_trace and "write_file" in llm.saw_trace, llm.saw_trace[:300])
check("and is told a local file does not count",
      "writing a local file" in llm.saw_trace)
check("the false 'I have solved' never reaches the screen",
      "I have solved" not in streamed, streamed)
check("the corrected answer is what the user sees",
      "did not solve" in streamed and "did not solve" in final, (streamed, final))

class OneStepLLM(ScriptedLLM):
    """One unverified tool call, then a claim - the shortcut that once skipped
    the check entirely."""
    def stream(self, messages, tools=None, think=False, max_tokens=None,
               temperature=None, stop_event=None):
        last = messages[-1].get("content") or ""
        if "Before answering" in last:
            self.saw_trace = last
            yield D("text", self.verdict)
        elif "Plan how to carry out" in last:
            yield D("text", "1. write it")
        else:
            self.n += 1
            if self.n == 1:
                yield D("tool_call", tool_call=ToolCall(
                    "write_file", {"path": "/tmp/x.md", "content": "x"}, "c1"))
            else:
                yield D("text", "I have solved LeetCode 3.")
        yield D("done")


llm1 = OneStepLLM("I only wrote a local file; nothing was submitted on LeetCode.")
streamed, final = run(llm1, "solve leetcode problem 3 for me")
check("a single unverified action is still checked before the user sees it",
      llm1.saw_trace and "did not" not in streamed and "only wrote" in final, (streamed, final))

llm = ScriptedLLM("CONFIRMED")
streamed, final = run(llm, "write a hello file for me")
check("a confirmed answer is shown unchanged",
      final == "I have solved LeetCode 3 and tested it." and streamed == final,
      (streamed, final))

REGISTRY.execute_many, REGISTRY.execute = real_exec, real_exec1

# ------------------------------------------------------------- web tooling
print("\n=== code editors on web pages ===")
from jarvis.tools import browser as B

CODE = "class Solution:\n    def f(self, x):\n        return {x: [1, 2]}\n"
real_js = B.run_js
B.run_js = lambda code, browser=None, timeout=40: (
    True, json.dumps({"ok": True, "kind": "monaco", "value": CODE}))
r = B.browser_set_editor(CODE)
check("code goes into the page's editor and is read back verbatim",
      r.ok and r.content["verified"] and r.content["editor"] == "monaco", r)
B.run_js = lambda code, browser=None, timeout=40: (
    True, json.dumps({"ok": True, "kind": "monaco", "value": "class Solution:\n  pass"}))
r = B.browser_set_editor(CODE)
check("an editor that ends up holding different code is a failure", not r.ok, r)
B.run_js = lambda code, browser=None, timeout=40: (True, json.dumps({"ok": False}))
check("a page with no editor says so", not B.browser_set_editor(CODE).ok)
check("the editor script handles quotes and newlines safely",
      json.dumps(CODE) in (B._EDITOR_JS % json.dumps(CODE)))

print("\n=== web pages without JavaScript ===")
from jarvis.tools import ax as AX

BLOCKED = (False, "Safari is blocking scripted JavaScript. Enable it once: ...")
B.run_js = lambda code, browser=None, timeout=40: BLOCKED
real_ax = (AX.available, AX.web_text, AX.web_controls, AX.press_web)
AX.available = lambda: True
AX.web_text = lambda app, n=8000: "Add Two Numbers\nYou are given two non-empty linked lists"
r = REGISTRY.get("browser_read").fn() if hasattr(REGISTRY.get("browser_read"), "fn") \
    else B.browser_read()
check("the whole page is read through accessibility, not pixels",
      r.ok and r.content.get("via") == "accessibility"
      and "linked lists" in r.content["text"], r.content)
AX.web_controls = lambda app, contains="", limit=40: [
    {"id": "w3", "role": "button", "text": "Run"},
    {"id": "w4", "role": "button", "text": "Submit"}]
r = B.browser_find()
check("page controls are listed through accessibility",
      r.ok and r.content["via"] == "accessibility" and r.content["count"] == 2, r.content)
pressed = []
AX.press_web = lambda app, text, occ=1: (pressed.append(text) or types.SimpleNamespace(
    ok=True, content={"pressed": text}, display="pressed", error=None))
r = B.browser_click("Submit")
check("buttons are pressed by name through accessibility", r.ok and pressed == ["Submit"])
AX.available, AX.web_text, AX.web_controls, AX.press_web = real_ax
B.run_js = real_js

print("\n=== paste keeps the clipboard ===")
import AppKit
from jarvis.tools import control as C


class FakeItem:
    def __init__(self, data): self.data = data
    def types(self): return list(self.data)
    def dataForType_(self, t): return self.data[t]


class FakePB:
    items = [FakeItem({"public.utf8-plain-text": b"the user's own clipboard"})]
    writes = []

    @classmethod
    def generalPasteboard(cls): return cls
    @classmethod
    def pasteboardItems(cls): return cls.items
    @classmethod
    def clearContents(cls): cls.writes.append("clear")
    @classmethod
    def setString_forType_(cls, s, t): cls.writes.append(("set", s))
    @classmethod
    def writeObjects_(cls, objs): cls.writes.append(("restore", len(objs)))


class FakeNSItem:
    @classmethod
    def alloc(cls): return cls()
    def init(self): self.d = {}; return self
    def setData_forType_(self, data, t): self.d[t] = data


real_pb = (AppKit.NSPasteboard, AppKit.NSPasteboardItem, C.press_key, C.focus_app)
AppKit.NSPasteboard, AppKit.NSPasteboardItem = FakePB, FakeNSItem
keys = []
C.press_key = lambda keys_=None, **k: keys.append(k.get("keys") or keys_)
C.focus_app = lambda app, timeout=3.0: (True, "TextEdit")
r = C.paste_text(text="print('hi')\n", app="TextEdit", replace=True)
check("pastes into the named app", r.ok and keys == ["cmd+a", "cmd+v"], keys)
check("the text goes on the clipboard only for the paste",
      ("set", "print('hi')\n") in FakePB.writes)
check("and the user's clipboard is put back afterwards",
      FakePB.writes[-1] == ("restore", 1), FakePB.writes)
AppKit.NSPasteboard, AppKit.NSPasteboardItem, C.press_key, C.focus_app = real_pb

print("\n=== playbooks ===")
r = REGISTRY.execute("playbook", {"name": "leetcode.com"})
steps = r.content.get("steps", "")
check("the LeetCode playbook is found from the site name", r.ok and "Accepted" in steps)
check("it defines done as LeetCode showing Accepted",
      "NOT solving it" in steps and "Submit" in steps)
check("it says to stop at a CAPTCHA rather than solve it",
      "Never solve a CAPTCHA" in steps)
check("an unknown site is not an error",
      REGISTRY.execute("playbook", {"name": "nowhere.example"}).ok)

if os.path.exists("/tmp/jarvis-honesty-test.md"):
    os.remove("/tmp/jarvis-honesty-test.md")
print(f"\n{sum(R)}/{len(R)} honesty and web checks passed")
sys.exit(0 if all(R) else 1)
