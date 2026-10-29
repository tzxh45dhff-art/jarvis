"""
Engine audit regressions: failures that left a request stuck, lost an answer,
or put the client out of step with the engine. No model, no network, no
screen: the model is a scripted fake and the daemon runs on a socketpair.

1. A model error mid-turn was followed by a "done" nobody read - the next
   request on the same connection got it as its answer.
2. A failed verification pass ended the turn with an error and the user never
   saw the (already finished) answer.
3. The verifier's second round recorded tool calls that never got results.
4. Trimming mid-job dropped the user's own request, and its fallback could
   keep two tool results with their call cut off.
5. "continue" right after "shall I continue?" started the paused song.
6. Stray confirm replies, stops during a confirmation prompt, and a reset
   during a request desynchronised or corrupted the conversation.
7. Azure: a stopped request still fell back to a second paid call; a rate
   limit wait ignored the stop; assistant -> assistant left calls open.
8. A confirmation channel that raised crashed the turn mid tool call.
9. A client whose engine died mid-request printed nothing at all.
"""
import io
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _isolate  # noqa: F401,E402

os.environ["JARVIS_ROUTER"] = "0"
os.environ["JARVIS_FOLLOWUPS"] = "0"

from jarvis.config import CONFIG  # noqa: E402
from jarvis.core import agent as agent_mod  # noqa: E402
from jarvis.core import fastpath, prompts  # noqa: E402
from jarvis.core.agent import Agent, Event  # noqa: E402
from jarvis.core.llm import Delta  # noqa: E402
from jarvis.core.protocol import ToolCall  # noqa: E402
from jarvis.daemon import server as srv  # noqa: E402
from jarvis.daemon.client import Client  # noqa: E402
from jarvis.daemon.protocol import Reader, send  # noqa: E402
from jarvis.safety.policy import Risk, Verdict  # noqa: E402
from jarvis.tools import REGISTRY  # noqa: E402
from jarvis.tools.registry import Tool, ToolResult  # noqa: E402

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {str(e)[:400]}"))
    R.append(bool(c))


# Nothing here may look at the screen, the players or the memory index.
prompts.turn_context = lambda: "[Current time: test]"
Agent._relevant_memories = lambda self, text: ""
import jarvis.context as _ctx  # noqa: E402
_ctx.front_window = lambda: ("Terminal", "")


class FakeLLM:
    """Each stream() call plays the next scripted step: a list of Deltas, or
    an exception to raise."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []

    def stream(self, messages, tools=None, think=False, max_tokens=None,
               temperature=None, stop_event=None):
        self.calls.append(len(messages))
        step = self.script.pop(0) if self.script else [Delta(kind="text", text="ok"),
                                                        Delta(kind="done")]
        if isinstance(step, Exception):
            raise step
        for d in step:
            yield d

    def warm(self, *a, **k):
        return 0.0


def text(t):
    return [Delta(kind="text", text=t), Delta(kind="done")]


def call(name, cid, args=None):
    return Delta(kind="tool_call", tool_call=ToolCall(name=name, arguments=args or {}, id=cid))


def run(ag, prompt):
    return list(ag.run(prompt))


def calls_answered(messages) -> bool:
    """Every assistant tool call is followed by its result before the next
    assistant message."""
    open_ids = set()
    for m in messages:
        if m.get("role") == "assistant":
            if open_ids:
                return False
            open_ids = {c.get("id") for c in m.get("tool_calls") or []}
        elif m.get("role") == "tool":
            open_ids.discard(m.get("tool_call_id"))
    return not open_ids


os.environ["JARVIS_FASTPATH"] = "0"

# ------------------------------------------------------------ model errors
print("=== a model error ends the turn ===")
ag = Agent(FakeLLM([RuntimeError("429 rate limited")]))
evs = run(ag, "what is the capital of france")
check("the last event is the error", evs and evs[-1].type == "error", [e.type for e in evs])
check("nothing follows the error (no unread 'done')",
      [e.type for e in evs].count("done") == 0, [e.type for e in evs])

ag = Agent(FakeLLM([[call("no_such_tool_xyz", "c1"), Delta(kind="done")],
                    RuntimeError("stream stalled")]))
evs = run(ag, "what is the capital of france")
check("an error after a tool step also ends the turn",
      evs[-1].type == "error" and not any(e.type == "done" for e in evs),
      [e.type for e in evs])

# ------------------------------------------------------------ verification
print("\n=== the verification pass ===")
ag = Agent(FakeLLM([text("Created notes.txt on the Desktop."), RuntimeError("boom")]))
evs = run(ag, "create a file called notes.txt on my desktop")
types = [e.type for e in evs]
check("a failed check is not shown as an error", "error" not in types, types)
check("the finished answer still reaches the user",
      any(e.type == "text" and "notes.txt" in e.text for e in evs), types)
check("'done' carries the answer", evs[-1].type == "done"
      and "notes.txt" in evs[-1].text, evs[-1])
check("the check's question is answered in the history",
      ag.messages[-1]["role"] == "assistant", ag.messages[-1])

ag = Agent(FakeLLM([
    text("Created notes.txt."),
    [call("no_such_tool_xyz", "v1"), Delta(kind="done")],          # round 0: a look
    [call("no_such_tool_xyz", "v2", {"again": 1}), Delta(kind="text", text="CONFIRMED"),
     Delta(kind="done")],                                            # round 1: may only answer
]))
evs = run(ag, "create a file called notes.txt")
check("round-two calls are not left unanswered in the history",
      calls_answered(ag.messages), [(m["role"], m.get("tool_calls")) for m in ag.messages[-4:]])
check("'CONFIRMED' in round two keeps the draft, never shows the word",
      evs[-1].type == "done" and evs[-1].text == "Created notes.txt.", evs[-1].text)

# ------------------------------------------------------------ the engine can start
print("\n=== what the engine needs at startup ===")
import jarvis as _pkg
check("jarvis.code_stamp exists (the engine imports it at startup and a stale stamp restarts it)",
      callable(getattr(_pkg, "code_stamp", None)) and isinstance(_pkg.code_stamp(), str), dir(_pkg))
import importlib
check("the daemon module imports cleanly", importlib.import_module("jarvis.daemon.server") is not None)
check("the package has a docstring", bool(_pkg.__doc__))

# ------------------------------------------------------------ trimming
print("\n=== trimming mid-job ===")
old_budget = CONFIG.llm.context_budget
CONFIG.llm.context_budget = 1000                 # ~3600 chars
try:
    ag = Agent(FakeLLM())
    ag.messages = [{"role": "system", "content": "sys"}]
    for i in range(4):
        ag.messages += [{"role": "user", "content": f"old question {i} " + "x" * 300},
                        {"role": "assistant", "content": f"old answer {i} " + "y" * 300}]
    req = {"role": "user", "content": "THE REQUEST: build the report"}
    ag.messages.append(req)
    ag._turn_msg = req
    for i in range(6):
        ag.messages += [{"role": "assistant", "content": "",
                         "tool_calls": [{"id": f"t{i}", "type": "function",
                                         "function": {"name": "fetch_page", "arguments": "{}"}}]},
                        {"role": "tool", "tool_call_id": f"t{i}", "name": "fetch_page",
                         "content": "z" * 900}]
    ag._trim()
    check("the request being worked on survives trimming",
          any(m is req for m in ag.messages), [m.get("content", "")[:30] for m in ag.messages])
    check("old history was dropped", len(ag.messages) < 15, len(ag.messages))
    check("no tool result is left without its call", calls_answered(ag.messages)
          and all(m["role"] != "tool" or ag.messages[i - 1]["role"] in ("assistant", "tool")
                  for i, m in enumerate(ag.messages)), [m["role"] for m in ag.messages])

    # The newest batch alone is over the target: keep it whole.
    ag = Agent(FakeLLM())
    ag.messages = [{"role": "system", "content": "sys"},
                   {"role": "user", "content": "q " * 2000},
                   {"role": "assistant", "content": "", "tool_calls": [
                       {"id": k, "type": "function", "function": {"name": "fetch_page",
                                                                   "arguments": "{}"}}
                       for k in ("a", "b", "c")]}] + [
        {"role": "tool", "tool_call_id": k, "name": "fetch_page", "content": "w" * 2500}
        for k in ("a", "b", "c")]
    ag._trim()
    roles = [m["role"] for m in ag.messages]
    check("a batch bigger than the target keeps its call",
          calls_answered(ag.messages) and "assistant" in roles
          and roles.index("assistant") < roles.index("tool"), roles)

    # Pages read for an earlier, answered request are shrunk when the history
    # is rebuilt; the current request's results are left whole.
    CONFIG.llm.context_budget = 4200
    ag = Agent(FakeLLM())
    ag.messages = [{"role": "system", "content": "sys"}]
    for i in range(3):
        ag.messages += [{"role": "user", "content": f"old q{i} " + "x" * 2000},
                        {"role": "assistant", "content": "", "tool_calls": [
                            {"id": f"o{i}", "type": "function", "function": {"name": "fetch_page", "arguments": "{}"}}]},
                        {"role": "tool", "tool_call_id": f"o{i}", "name": "fetch_page", "content": "p" * 3000},
                        {"role": "assistant", "content": f"answer {i}"}]
    req = {"role": "user", "content": "now this"}
    ag.messages.append(req)
    ag._turn_msg = req
    ag.messages += [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "n1", "type": "function", "function": {"name": "fetch_page", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "n1", "name": "fetch_page", "content": "N" * 3000}]
    ag._trim()
    olds = [m for m in ag.messages if m.get("role") == "tool" and m["content"].startswith("p")]
    news = [m for m in ag.messages if m.get("role") == "tool" and m["content"].startswith("N")]
    check("an earlier request's page is shrunk when history is rebuilt",
          olds and all(len(m["content"]) < 1700 and "cut" in m["content"] for m in olds),
          [len(m["content"]) for m in olds])
    check("the current request's results stay whole", news and len(news[0]["content"]) == 3000)
finally:
    CONFIG.llm.context_budget = old_budget

# ------------------------------------------------------------ fast path
print("\n=== 'continue' after a question ===")
orig_mc, orig_front = fastpath._music_context, fastpath._music_in_front
fastpath._music_context = lambda: True           # a song is paused somewhere
fastpath._music_in_front = lambda: False
check("bare 'continue' with music only in the background goes to the model",
      fastpath.action("continue") is None, fastpath.action("continue"))
check("bare 'resume' likewise", fastpath.action("resume") is None)
check("'play' still runs directly", fastpath.action("play") == ("play_music", {}))
check("'resume the music' still runs directly",
      fastpath.action("resume the music") == ("play_music", {}))
fastpath._music_in_front = lambda: True
check("'continue' with Spotify in front resumes it",
      fastpath.action("continue") == ("play_music", {}))

ag = Agent(FakeLLM())
ag.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "do the steps"},
               {"role": "assistant", "content": "Steps 1-3 are done. Shall I continue?"}]
check("a question is awaiting an answer", ag._awaiting_reply())
ag.messages[-1]["content"] = "[Stopped by the user before finishing.]"
check("a stopped job is awaiting an answer", ag._awaiting_reply())
ag.messages[-1]["content"] = "Done."
check("a plain answer is not", not ag._awaiting_reply())

ran = []
orig_exec = REGISTRY.execute
REGISTRY.execute = lambda n, a, **k: (ran.append((n, a)) or
                                      ToolResult(True, content={"level": 30}, display="volume 30"))
os.environ["JARVIS_FASTPATH"] = "1"
logged = []


class _Grab(logging.Handler):
    def emit(self, rec):
        logged.append(rec.getMessage())


agent_mod.log.addHandler(_Grab())
agent_mod.log.setLevel(logging.INFO)
try:
    llm = FakeLLM([text("Carrying on with step 4.")])
    ag = Agent(llm)
    ag.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "do the steps"},
                   {"role": "assistant", "content": "Steps 1-3 are done. Shall I continue?"}]
    evs = run(ag, "continue")
    check("'continue' after a question is answered by the model, not the player",
          not ran and llm.calls, (ran, llm.calls))

    notes = []
    ag = Agent(FakeLLM())
    ag._learner.note = lambda u, reply="": notes.append((u, reply))
    run(ag, "set volume to 30")
    check("a fast-path tool call is logged", any("[fastpath]" in m and "set_volume" in m
                                                for m in logged), logged[-3:])
    ag = Agent(FakeLLM([text("Paris is the capital.")]))
    ag._learner.note = lambda u, reply="": notes.append((u, reply))
    os.environ["JARVIS_FASTPATH"] = "0"
    run(ag, "what is the capital of france")
    check("the learner gets the reply as context",
          notes and notes[-1] == ("what is the capital of france", "Paris is the capital."), notes)
finally:
    REGISTRY.execute = orig_exec
    os.environ["JARVIS_FASTPATH"] = "0"
    fastpath._music_context, fastpath._music_in_front = orig_mc, orig_front

# ------------------------------------------------------------ registry
print("\n=== confirmation channel failures ===")
did = []
REGISTRY.register(Tool("audit_dummy_delete", "test only", {"type": "object", "properties": {}},
                       lambda: did.append(1) or "ran", risk=Risk.HIGH, category="delete"))
old_hook = REGISTRY.confirm_hook


def _broken(*a):
    raise OSError("socket closed")


REGISTRY.confirm_hook = _broken
try:
    r = REGISTRY.execute("audit_dummy_delete", {})
    check("a broken confirmation channel declines instead of crashing",
          not r.ok and "Declined" in r.error and not did, r)
finally:
    REGISTRY.confirm_hook = old_hook
    REGISTRY._tools.pop("audit_dummy_delete", None)

# ------------------------------------------------------------ azure
print("\n=== azure backend ===")
from jarvis.core import azure as az  # noqa: E402

msgs = az.AzureBackend._to_messages([
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "a1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
    {"role": "assistant", "content": "hello"},
    {"role": "user", "content": "next"}])
check("assistant -> assistant: the open call is answered in between",
      [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant", "user"],
      [m["role"] for m in msgs])

b = az.AzureBackend(endpoint="https://example.invalid/", deployment="d")
posted = []


def _nothing(*a, **k):
    return iter(())


def _once(body, stop_event, produced):
    posted.append(1)
    yield Delta(kind="done")


b._stream_responses = _nothing
b._stream_once = _once
stop = threading.Event()
stop.set()
list(b.stream([{"role": "user", "content": "x"}], think=True, stop_event=stop))
check("a stopped request makes no fallback call", not posted, posted)

err = urllib.error.HTTPError("u", 429, "Too Many Requests", {"retry-after": "30"},
                             io.BytesIO(b'{"error": {"message": "rate limited"}}'))


def _limited(body, stop_event, produced):
    posted.append(1)
    raise err
    yield  # noqa


b._stream_once = _limited
posted.clear()
stop = threading.Event()
threading.Timer(0.3, stop.set).start()
t0 = time.time()
list(b.stream([{"role": "user", "content": "x"}], stop_event=stop))
check("a stop ends a rate-limit wait at once", time.time() - t0 < 3 and len(posted) == 1,
      f"{time.time() - t0:.1f}s, {len(posted)} call(s)")

# ------------------------------------------------------------ daemon
print("\n=== daemon protocol ===")


class ScriptAgent:
    def __init__(self, fn):
        self.fn = fn
        self.messages = []
        self.session_id = "t"
        self._turn_index = 0
        self.follow_ups = False
        self._last_followup_turn = -99
        self._trace = []
        self.stop_event = threading.Event()

    def run(self, text, voice=False):
        yield from self.fn(self)

    def interrupt(self):
        self.stop_event.set()

    def reset(self):
        self.messages = []


def daemon(agent):
    d = srv.Daemon.__new__(srv.Daemon)
    d.code, d.backend, d.agent = "test", FakeLLM(), agent
    d.ready = threading.Event()
    d.ready.set()
    d.status = "ready"
    d._sock, d._stt, d._lock_fh, d._turn, d._turn_seq = None, None, None, None, 0
    d._stop, d._stop_listening, d._request_pending = (threading.Event(), threading.Event(),
                                                      threading.Event())
    d._busy, d._listen_lock = threading.Lock(), threading.Lock()
    return d


def connect(d):
    a, b = socket.socketpair()
    threading.Thread(target=d._handle, args=(b,), daemon=True).start()
    a.settimeout(10)
    return a, Reader(a)


def until_end(rd):
    out = []
    for m in rd.messages():
        out.append(m)
        if m.get("type") in ("done", "error"):
            break
    return out


def old_agent(self):
    yield Event("text", text="hi")
    yield Event("error", text="Model error: x")
    yield Event("done", text="late")


d = daemon(ScriptAgent(old_agent))
c, rd = connect(d)
send(c, {"type": "run", "text": "x"})
got = until_end(rd)
send(c, {"type": "ping"})
nxt = rd.next_message(timeout=5) or {}
check("after an 'error' nothing more is sent for that request",
      got[-1]["type"] == "error" and nxt.get("type") == "pong", (got, nxt))
c.close()

d = daemon(ScriptAgent(old_agent))
c, rd = connect(d)
send(c, {"type": "confirm_reply", "approve": True})
send(c, {"type": "ping"})
nxt = rd.next_message(timeout=5) or {}
check("a late confirm reply gets no 'unknown request' error", nxt.get("type") == "pong", nxt)
c.close()

answers = []


def asks(self):
    v = Verdict(Risk.HIGH, "delete things", category="delete", needs_confirm=True)
    answers.append(REGISTRY.confirm_hook("trash_files", {}, v))
    yield Event("done", text="finished")


d = daemon(ScriptAgent(asks))
c, rd = connect(d)
send(c, {"type": "run", "text": "delete it"})
first = rd.next_message(timeout=5) or {}
t0 = time.time()
d._cancel_turn("stopped by the user")
got = until_end(rd)
send(c, {"type": "ping"})
nxt = rd.next_message(timeout=5) or {}
check("a stop during a confirmation prompt ends the request at once",
      first.get("type") == "confirm" and got and got[-1].get("meta", {}).get("stopped")
      and time.time() - t0 < 3, (first, got))
check("the next request on that connection is not eaten by the old prompt",
      nxt.get("type") == "pong", nxt)
time.sleep(0.8)
check("the stopped prompt counted as 'no'", answers == [False], answers)
c.close()


def long_job(self):
    self.messages.append({"role": "user", "content": "long job"})
    while not self.stop_event.wait(0.05):
        pass
    yield Event("done", text="")


ag0 = ScriptAgent(long_job)
ag0.messages = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}]
d = daemon(ag0)
c, rd = connect(d)
send(c, {"type": "run", "text": "long job"})
time.sleep(0.4)
c2, rd2 = connect(d)
send(c2, {"type": "reset"})
ok = rd2.next_message(timeout=8) or {}
got = until_end(rd)
check("reset during a request stops it first", ok.get("type") == "reset_ok"
      and got and got[-1].get("meta", {}).get("stopped"), (ok, got))
check("and the conversation really is empty afterwards", d.agent.messages == [],
      d.agent.messages)
c.close()
c2.close()

# ------------------------------------------------------------ client
print("\n=== client when the engine dies mid-request ===")
a, b = socket.socketpair()


def dies():
    Reader(b).next_message(timeout=5)
    send(b, {"type": "status", "text": "working"})
    b.close()


threading.Thread(target=dies, daemon=True).start()
cl = Client(autostart=False)
cl.sock, cl.reader = a, Reader(a)
evs = list(cl.run("hello"))
check("a dead engine is reported, not silent",
      evs and evs[-1]["type"] == "error" and "lost connection" in evs[-1]["text"], evs)
check("the dead connection is dropped so the next request reconnects", cl.sock is None)

print(f"\n{sum(R)}/{len(R)} engine-audit checks passed")
sys.exit(0 if all(R) else 1)
