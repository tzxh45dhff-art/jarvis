"""
Going in circles, and knowing a video is really playing.

Regression cover for a real run: "resume the episode of The Mentalist" made 32
tool calls over three minutes - reopening the same Netflix page, searching again,
clicking Play again - because the browser blocked scripting and nothing could
confirm playback.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _isolate  # noqa: F401  (keeps tests out of the user's memory)

from jarvis.core import agent as A
from jarvis.core.agent import Agent
from jarvis.core.protocol import ToolCall
from jarvis.tools import REGISTRY, ToolResult

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {str(e)[:300]}"))
    R.append(bool(c))


def D(kind, text="", tool_call=None):
    return types.SimpleNamespace(kind=kind, text=text, tool_call=tool_call,
                                 n_tokens=1, tps=1, prompt_tps=1)


class LoopingLLM:
    """Keeps calling a different-looking tool every step, forever."""
    _kind = "azure"          # a remote backend, where reasoning costs seconds

    def __init__(self):
        self.n = 0
        self.think_flags = []
        self.saw_nudge = False
        self.stop_prompt = False

    def stream(self, messages, tools=None, think=False, max_tokens=None,
               temperature=None, stop_event=None):
        last = messages[-1].get("content") or ""
        if "Plan how to carry out" in last:
            yield D("text", "1. do it")
            yield D("done")
            return
        if "Before answering" in last:
            yield D("text", "CONFIRMED")
            yield D("done")
            return
        if any("without confirming progress" in (m.get("content") or "") for m in messages
               if m.get("role") == "user"):
            self.saw_nudge = True
        if tools is None:
            self.stop_prompt = True
            yield D("text", "I opened the page and searched, but could not confirm playback.")
            yield D("done")
            return
        self.n += 1
        self.think_flags.append(bool(think))
        yield D("tool_call", tool_call=ToolCall("browser_read", {"max_chars": 1000 + self.n}, f"c{self.n}"))
        yield D("done")


def stub_tools(content):
    def one(name, args, **k):
        return ToolResult(True, content=dict(content), display="read page")
    REGISTRY.execute = one
    REGISTRY.execute_many = lambda pairs: [one(n, a) for n, a in pairs]


real_exec, real_many = REGISTRY.execute, REGISTRY.execute_many


def run(llm, task):
    ag = Agent(llm)
    ag._relevant_memories = lambda t: ""
    ag._auto_capture = lambda a, b: None
    ag.follow_ups = False
    statuses, final = [], ""
    for ev in ag.run(task, max_steps=40):
        if ev.type == "status":
            statuses.append(ev.text)
        elif ev.type == "done":
            final = ev.text
    return statuses, final


print("=== a job that never confirms anything stops itself ===")
stub_tools({"text": "page"})
llm = LoopingLLM()
statuses, final = run(llm, "open the mentalist and resume the episode")
check("it does not run to the 40-step budget", llm.n <= A.STALL_STOP + 1, llm.n)
check("it is nudged once before stopping", llm.saw_nudge and
      any("going in circles" in s for s in statuses), statuses)
check("then it stops and explains", llm.stop_prompt and "could not confirm" in final, (llm.stop_prompt, final))
check("and says why in the status line", any("no confirmed progress" in s for s in statuses), statuses)

print("\n=== confirmed progress is not a stall ===")
stub_tools({"verified": True, "note": "playing"})
llm = LoopingLLM()
statuses, final = run(llm, "open the mentalist and resume the episode")
check("a confirmed result keeps the job going past the stall limit", llm.n > A.STALL_STOP, llm.n)
check("and nothing is called a stall", not any("circles" in s for s in statuses), statuses)

print("\n=== reasoning is spent only where it helps ===")
stub_tools({"text": "page"})
llm = LoopingLLM()
Agent._should_think = lambda self, t: True          # a 'hard' task
statuses, final = run(llm, "research and build a plan and do all of it step by step")
flags = llm.think_flags
check("the first step reasons", flags and flags[0] is True, flags[:5])
check("routine steps after clean results do not", not any(flags[1:6]), flags[:8])
stub_tools({"warning": "this is not confirmed", "text": "x"})
llm = LoopingLLM()
run(llm, "research and build a plan and do all of it step by step")
check("a step that came back with a warning gets full reasoning next",
      len(llm.think_flags) > 2 and llm.think_flags[1] is True, llm.think_flags[:6])
REGISTRY.execute, REGISTRY.execute_many = real_exec, real_many

print("\n=== the final check only looks, and only when it is needed ===")


class OneThenDone:
    _kind = "azure"

    def __init__(self, tool, args):
        self.tool, self.args, self.n = tool, args, 0
        self.check_tools = None
        self.checked = False

    def stream(self, messages, tools=None, think=False, max_tokens=None,
               temperature=None, stop_event=None):
        last = messages[-1].get("content") or ""
        if "Plan how to carry out" in last:
            yield D("text", "1. do it"); yield D("done"); return
        if "Before answering" in last:
            self.checked = True
            self.check_tools = [t["function"]["name"] for t in (tools or [])]
            yield D("text", "CONFIRMED"); yield D("done"); return
        self.n += 1
        if self.n == 1:
            yield D("tool_call", tool_call=ToolCall(self.tool, self.args, "c1"))
        else:
            yield D("text", "Done - it is playing.")
        yield D("done")


stub_tools({"verified": True, "media_playing": True})
llm = OneThenDone("browser_open", {"url": "https://www.netflix.com/watch/1"})
run(llm, "open netflix and play the mentalist")
check("a job whose last step confirmed itself is not re-checked", not llm.checked)

stub_tools({"text": "saved"})
llm = OneThenDone("write_file", {"path": "/tmp/x", "content": "y"})
run(llm, "create a file with my notes and open it")
check("an unconfirmed change is checked", llm.checked)
check("...offered the same tools as the job, so the prompt cache holds "
      "(look-only is enforced when calls run - see test_speed_paths)",
      llm.check_tools is not None and len(llm.check_tools) == len(REGISTRY.schemas()),
      (len(llm.check_tools or []), len(REGISTRY.schemas())))

stub_tools({"text": "Canberra population 484,630"})
llm = OneThenDone("fetch_page", {"url": "https://en.wikipedia.org/wiki/Canberra"})
run(llm, "open the canberra page and tell me the population")
check("a pure look-up is not re-checked (no extra model call)", not llm.checked)
REGISTRY.execute, REGISTRY.execute_many = real_exec, real_many

print("\n=== playback is detected without running page scripts ===")
from jarvis.tools import browser as B
from jarvis.tools import media as M

seq = {"calls": 0}
before_ids = {"0xaaa"}


def fake_ids():
    seq["calls"] += 1
    return set(before_ids) if seq["calls"] == 1 else before_ids | {"0xbbb"}


M.browser_media_ids = fake_ids
B._open_in_owned_tab = lambda app, url: "reused"
B._wait_loaded = lambda app, host, cap=8.0: True
B._page_snapshot = lambda app, max_chars=1500: {"title": "Netflix", "text": "page"}
B.run_js = lambda code, app=None, timeout=40: (False, "Safari is blocking scripted JavaScript")
B._active_browser = lambda: "Safari"
real_sleep = B.time.sleep
B.time.sleep = lambda s: None
r = B.browser_open("https://www.netflix.com/watch/80014343?trackId=1")
check("a NEW media session means it is playing", r.content.get("media_playing") is True, r.content)
check("and the result says it is verified", r.content.get("verified") is True)
check("and tells the model not to re-check", "no need to reload" in r.content.get("note", ""))

seq["calls"] = 0
M.browser_media_ids = lambda: {"0xaaa"}             # only the pre-existing one
r = B.browser_open("https://www.netflix.com/watch/80014343", wait=0)
check("an old session from another tab is not counted", r.content.get("media_playing") is False, r.content)
check("not playing is not 'verified'", not r.content.get("verified"))
M.browser_media_ids = lambda: None
r = B.browser_open("https://www.youtube.com/watch?v=abc")
check("if the system cannot be asked it says unknown, not false", r.content.get("media_playing") is None, r.content)
r = B.browser_open("https://example.com/watch/1")
check("ordinary sites are untouched", "media_playing" not in r.content, r.content)
r = B.browser_open("https://www.netflix.com/browse")
check("a Netflix page that is not a video is untouched too", "media_playing" not in r.content, r.content)
B.time.sleep = real_sleep

M.browser_media_ids = lambda: {"0x1"}
check("video_state: playing", REGISTRY.execute("video_state", {}).content["playing"] is True)
M.browser_media_ids = lambda: set()
check("video_state: not playing", REGISTRY.execute("video_state", {}).content["playing"] is False)

print("\n=== know-how is findable ===")
for name in ("Netflix", "youtube", "Prime Video", "Hotstar", "Spotify"):
    r = REGISTRY.execute("playbook", {"name": name})
    check(f"playbook for {name}", r.ok and r.content.get("playbook") == "streaming", r.content)
check("it tells the model not to reload to check",
      "Do not" in REGISTRY.execute("playbook", {"name": "netflix"}).content["steps"])

print(f"\n{sum(R)}/{len(R)} stall and video checks passed")
sys.exit(0 if all(R) else 1)
