"""
Claude backend: message translation, tool schemas, SSE parsing, switching.

No network and no API credit needed — the HTTP layer is stubbed with a real
recorded SSE stream, so this exercises exactly the code paths a live call does.
The one thing it cannot prove is that the account can pay; a live call is
checked separately by `--live`.
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.core.anthropic import AnthropicBackend

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


b = AnthropicBackend(key="test-key")

# --------------------------------------------------------------- tool schemas
print("=== tool schemas ===")
from jarvis.tools import REGISTRY

tools = b._to_tools(REGISTRY.schemas(include_deferred=True))
check(f"every tool converts ({len(tools)})", len(tools) == len(REGISTRY.names()))
check("uses input_schema, not parameters",
      all("input_schema" in t and "parameters" not in t for t in tools))
check("every schema is an object type",
      all(t["input_schema"].get("type") == "object" for t in tools),
      [t["name"] for t in tools if t["input_schema"].get("type") != "object"])
check("names round-trip", {t["name"] for t in tools} == set(REGISTRY.names()))
check("a cache breakpoint is set on the tool block",
      tools[-1].get("cache_control") == {"type": "ephemeral"})

# ---------------------------------------------------------------- translation
print("\n=== message translation ===")
convo = [
    {"role": "system", "content": "You are JARVIS."},
    {"role": "user", "content": "what's on my calendar?"},
    {"role": "assistant", "content": "Checking.",
     "tool_calls": [{"id": "toolu_1", "type": "function",
                     "function": {"name": "check_calendar",
                                  "arguments": {"when": "today"}}}]},
    {"role": "tool", "tool_call_id": "toolu_1", "name": "check_calendar",
     "content": '{"count": 0}'},
    {"role": "assistant", "content": "Nothing today."},
    {"role": "user", "content": "thanks"},
]
msgs, system = b._to_messages(convo)
check("system is lifted out", system == "You are JARVIS.")
check("roles alternate strictly",
      all(msgs[i]["role"] != msgs[i + 1]["role"] for i in range(len(msgs) - 1)),
      [m["role"] for m in msgs])
check("starts on a user turn", msgs[0]["role"] == "user")
tool_use = [c for m in msgs for c in m["content"] if c["type"] == "tool_use"]
tool_res = [c for m in msgs for c in m["content"] if c["type"] == "tool_result"]
check("the tool call became a tool_use block", len(tool_use) == 1)
check("the result became a tool_result block", len(tool_res) == 1)
check("ids pair up", tool_use[0]["id"] == tool_res[0]["tool_use_id"] == "toolu_1")

print("\n=== a trimmed conversation is repaired, not rejected ===")
# history trimming can drop either half of a call/result pair
orphan_result = [
    {"role": "system", "content": "s"},
    {"role": "tool", "tool_call_id": "gone", "name": "check_calendar",
     "content": "{}"},
    {"role": "user", "content": "and now?"},
]
msgs, _ = b._to_messages(orphan_result)
ids = {c.get("tool_use_id") for m in msgs for c in m["content"]}
check("an orphaned result carries no dangling tool_use_id", ids == {None},
      ids)
check("its content survives as text",
      any("check_calendar" in c.get("text", "")
          for m in msgs for c in m["content"]), msgs)

orphan_call = [
    {"role": "system", "content": "s"},
    {"role": "user", "content": "go"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"id": "toolu_9", "type": "function",
                     "function": {"name": "run_shell",
                                  "arguments": {"command": "ls"}}}]},
]
msgs, _ = b._to_messages(orphan_call)
uses = {c["id"] for m in msgs for c in m["content"] if c["type"] == "tool_use"}
res = {c["tool_use_id"] for m in msgs for c in m["content"]
       if c["type"] == "tool_result"}
check("an interrupted call gets a synthetic result", uses == res == {"toolu_9"},
      (uses, res))
check("roles still alternate after repair",
      all(msgs[i]["role"] != msgs[i + 1]["role"] for i in range(len(msgs) - 1)),
      [m["role"] for m in msgs])

print("\n=== parallel tool calls in one turn ===")
par = [
    {"role": "system", "content": "s"},
    {"role": "user", "content": "check both"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "a", "type": "function",
         "function": {"name": "check_calendar", "arguments": {}}},
        {"id": "b", "type": "function",
         "function": {"name": "check_reminders", "arguments": {}}}]},
    {"role": "tool", "tool_call_id": "a", "name": "check_calendar", "content": "{}"},
    {"role": "tool", "tool_call_id": "b", "name": "check_reminders", "content": "{}"},
]
msgs, _ = b._to_messages(par)
last = msgs[-1]
check("both results land in one user turn",
      last["role"] == "user"
      and sum(c["type"] == "tool_result" for c in last["content"]) == 2,
      last)

print("\n=== an empty conversation still produces a valid request ===")
msgs, _ = b._to_messages([{"role": "system", "content": "s"}])
check("falls back to a user turn",
      len(msgs) == 1 and msgs[0]["role"] == "user", msgs)

# ------------------------------------------------------------------ SSE parse
print("\n=== streaming ===")
SSE = b"""event: message_start
data: {"type":"message_start","message":{"usage":{"input_tokens":1200,"cache_read_input_tokens":4800,"cache_creation_input_tokens":0}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Checking"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" now."}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: content_block_start
data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_x","name":"check_calendar","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"when\\":"}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":" \\"today\\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":42}}

event: message_stop
data: {"type":"message_stop"}
"""

captured = {}


class FakeResp(io.BytesIO):
    def close(self):
        pass


def fake_post(body, timeout=120, stream=False):
    captured.update(body)
    return FakeResp(SSE)


b._post = fake_post
text, calls, done = [], [], None
for d in b.stream(convo, tools=REGISTRY.schemas()):
    if d.kind == "text":
        text.append(d.text)
    elif d.kind == "tool_call":
        calls.append(d.tool_call)
    elif d.kind == "done":
        done = d

check("text streams incrementally", len(text) == 2 and "".join(text)
      == "Checking now.", text)
check("the tool call is reassembled from partial JSON",
      len(calls) == 1 and calls[0].name == "check_calendar"
      and calls[0].arguments == {"when": "today"},
      [(c.name, c.arguments) for c in calls])
check("the tool_use id is preserved for the result to reference",
      calls[0].id == "toolu_x", calls[0].id)
check("output tokens are reported", done and done.n_tokens == 42, done)
check("cache reads are tallied", b.usage["cache_read"] == 4800, b.usage)

print("\n=== the request body itself ===")
check("streaming is requested", captured.get("stream") is True)
check("the cheapest capable model is the default",
      captured.get("model") == "claude-haiku-4-5-20251001", captured.get("model"))
check("system carries the cache breakpoint (it covers tools + system)",
      captured["system"][0].get("cache_control") == {"type": "ephemeral"},
      captured.get("system"))
check("temperature is 0 when tools are offered",
      captured.get("temperature") == 0.0, captured.get("temperature"))
check("a second breakpoint caches the conversation tail",
      sum("cache_control" in c
          for m in captured["messages"] for c in m["content"]) == 1)
check("no more than four cache breakpoints (the API limit)",
      1 + 1 + sum("cache_control" in c for m in captured["messages"]
                  for c in m["content"]) <= 4)

# ------------------------------------------------------------------ switching
print("\n=== backend selection ===")
import jarvis.core.llm as L

for alias, want in [("claude", "claude"), ("anthropic", "claude"),
                    ("local", "local"), ("gemma", "local"),
                    ("cloud", "cloud"), ("gemini", "cloud"), ("junk", "")]:
    check(f"{alias!r} resolves to {want!r}", L.resolve_kind(alias) == want)

os.environ.pop("JARVIS_BACKEND", None)
check("an Anthropic key alone never makes Claude the default",
      L.default_kind() != "claude", L.default_kind())
os.environ["JARVIS_BACKEND"] = "claude"
check("JARVIS_BACKEND opts in explicitly", L.default_kind() == "claude")
os.environ.pop("JARVIS_BACKEND")

L._BACKEND = None
_had_key = os.environ.get("ANTHROPIC_API_KEY")
os.environ["ANTHROPIC_API_KEY"] = _had_key or "test-key"      # the Claude backend needs *a* key to exist
be = L.get_backend("claude")
check("get_backend('claude') returns the Claude backend",
      type(be).__name__ == "AnthropicBackend" and be._kind == "claude")
check("it needs no local weights", L.is_remote(be))
check("it is labelled by model", "haiku" in L.backend_name(be),
      L.backend_name(be))
check("it satisfies the backend contract",
      all(callable(getattr(be, m, None)) for m in
          ("stream", "load", "warm", "touch_weights", "count_tokens",
           "describe_images", "complete", "unload")))
L._BACKEND = None

print(f"\n{sum(R)}/{len(R)} Claude backend checks passed")
sys.exit(0 if all(R) else 1)
