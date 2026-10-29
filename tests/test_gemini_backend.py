"""
Gemini backend: translation, thought signatures, streaming, retries, defaults.

Offline - the HTTP layer is replaced with recorded responses, so this runs in a
second, costs nothing, and covers the failure paths a live call rarely hits.
The behaviours asserted here were each confirmed against the live API first
(October 2026), most importantly that a step-2 request without the step-1
thought signature is rejected with HTTP 400.
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jarvis.core.cloud as C
from jarvis.core.cloud import BYPASS_SIGNATURE, GeminiBackend, _HTTPError
from jarvis.core.protocol import ToolCall

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


C.time.sleep = lambda s: None          # retries must not slow the suite down
b = GeminiBackend(key="test-key")

# --------------------------------------------------------------- tool schemas
print("=== tool declarations ===")
from jarvis.tools import REGISTRY

decls = b._to_declarations(REGISTRY.schemas(include_deferred=True))[0]["functionDeclarations"]
check(f"every tool converts ({len(decls)})", len(decls) == len(REGISTRY.names()))
check("schemas pass through untouched as JSON Schema",
      all(d["parametersJsonSchema"] == REGISTRY.get(d["name"]).parameters
          for d in decls))
check("no lossy OpenAPI conversion field is used",
      not any("parameters" in d for d in decls))

# ---------------------------------------------------------------- translation
print("\n=== message translation ===")
convo = [
    {"role": "system", "content": "You are JARVIS."},
    {"role": "user", "content": "battery and weather?"},
    {"role": "assistant", "content": "Checking.", "tool_calls": [
        {"id": "call_1", "type": "function", "thought_signature": "SIG-A",
         "function": {"name": "system_status", "arguments": {}}},
        {"id": "call_2", "type": "function",
         "function": {"name": "get_weather", "arguments": {"location": "Paris"}}}]},
    {"role": "tool", "tool_call_id": "call_1", "name": "system_status",
     "content": '{"ok": true, "content": {"battery": "61%"}}'},
    {"role": "tool", "tool_call_id": "call_2", "name": "get_weather",
     "content": '{"ok": true, "content": "18C"}'},
    {"role": "assistant", "content": "61% and 18C."},
    {"role": "user", "content": "thanks"},
]
contents, system = b._to_contents(convo)
check("system prompt is lifted out", system == "You are JARVIS.")
check("roles alternate", all(contents[i]["role"] != contents[i + 1]["role"]
                             for i in range(len(contents) - 1)),
      [c["role"] for c in contents])
calls = [p for c in contents for p in c["parts"] if "functionCall" in p]
check("the signature goes back verbatim on the call that carried it",
      calls[0].get("thoughtSignature") == "SIG-A", calls[0])
check("a second parallel call gets no invented signature",
      "thoughtSignature" not in calls[1], calls[1])
check("call ids are preserved", [c["functionCall"]["id"] for c in calls]
      == ["call_1", "call_2"])
resp = [p for c in contents for p in c["parts"] if "functionResponse" in p]
check("parallel results share one user turn",
      any(sum("functionResponse" in p for p in c["parts"]) == 2
          for c in contents))
check("results are paired to their calls by id",
      [r["functionResponse"]["id"] for r in resp] == ["call_1", "call_2"])
check("the JSON tool result arrives as an object, not a string",
      isinstance(resp[0]["functionResponse"]["response"]["output"], dict),
      resp[0])

print("\n=== history from another model ===")
foreign = [
    {"role": "system", "content": "s"},
    {"role": "user", "content": "go"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "g1", "function": {"name": "list_dir", "arguments": {}}},
        {"id": "g2", "function": {"name": "file_info", "arguments": {}}}]},
    {"role": "tool", "tool_call_id": "g1", "name": "list_dir", "content": "{}"},
    {"role": "tool", "tool_call_id": "g2", "name": "file_info", "content": "{}"},
]
contents, _ = b._to_contents(foreign)
calls = [p for c in contents for p in c["parts"] if "functionCall" in p]
check("an unsigned call from Gemma gets Google's bypass value",
      calls[0].get("thoughtSignature") == BYPASS_SIGNATURE, calls[0])
check("only on the first call of the step, as the API expects",
      "thoughtSignature" not in calls[1])
contents, _ = b._to_contents(convo, bypass_all=True)
calls = [p for c in contents for p in c["parts"] if "functionCall" in p]
check("bypass mode replaces even a real signature (for a fallback model)",
      calls[0].get("thoughtSignature") == BYPASS_SIGNATURE)

print("\n=== trimmed or interrupted history is repaired, not rejected ===")
orphan = [{"role": "system", "content": "s"},
          {"role": "tool", "tool_call_id": "gone", "name": "list_dir",
           "content": "{}"},
          {"role": "user", "content": "and now?"}]
contents, _ = b._to_contents(orphan)
check("an orphaned result becomes text",
      not any("functionResponse" in p for c in contents for p in c["parts"])
      and "list_dir" in json.dumps(contents), contents)
check("and the conversation still starts with the user",
      contents[0]["role"] == "user")
dangling = [{"role": "system", "content": "s"},
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "x9", "function": {"name": "run_shell",
                                          "arguments": {"command": "ls"}}}]},
            {"role": "user", "content": "never mind"}]
contents, _ = b._to_contents(dangling)
resp = [p["functionResponse"] for c in contents for p in c["parts"]
        if "functionResponse" in p]
check("an interrupted call gets a synthetic error result",
      len(resp) == 1 and resp[0]["id"] == "x9" and "error" in resp[0]["response"],
      resp)
check("roles still alternate after repair",
      all(contents[i]["role"] != contents[i + 1]["role"]
          for i in range(len(contents) - 1)), [c["role"] for c in contents])
contents, _ = b._to_contents([{"role": "system", "content": "s"}])
check("an empty conversation is still a valid request",
      contents == [{"role": "user", "parts": [{"text": "Hello."}]}])

# ---------------------------------------------------------------- request body
print("\n=== request body ===")
tools = REGISTRY.schemas()
body = b._body(convo, tools, False, None, None)
check("no temperature unless asked (Gemini 3 loops when it is lowered)",
      "temperature" not in body["generationConfig"])
check("explicit temperature is honoured",
      b._body(convo, tools, False, None, 0.2)["generationConfig"]["temperature"] == 0.2)
check("default output budget fits a long document in a tool argument",
      body["generationConfig"]["maxOutputTokens"] >= C.MIN_OUTPUT_TOKENS)
check("a short explicit limit is respected",
      b._body(convo, tools, False, 500, None)["generationConfig"]["maxOutputTokens"] == 500)
check("deep reasoning maps to thinkingLevel=high",
      b._body(convo, tools, True, None, None)["generationConfig"]
      .get("thinkingConfig") == {"thinkingLevel": "high"})
wrap = b._body(convo, None, False, 500, None)
check("a wrap-up turn over tool history forbids new calls",
      wrap.get("toolConfig") == {"functionCallingConfig": {"mode": "NONE"}}, wrap.get("toolConfig"))
plain = GeminiBackend(key="k")._body([{"role": "user", "content": "hi"}], None,
                                     False, None, None)
check("a plain chat sends no tools at all", "tools" not in plain)

# -------------------------------------------------------------------- stream
print("\n=== streaming ===")
SSE = b"""data: {"candidates":[{"content":{"role":"model","parts":[{"text":"Check"}]}}]}

data: {"candidates":[{"content":{"role":"model","parts":[{"text":"ing."}]}}]}

data: {"candidates":[{"content":{"role":"model","parts":[{"functionCall":{"name":"system_status","args":{},"id":"call_77"},"thoughtSignature":"SIG-Z"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":7600,"cachedContentTokenCount":7000,"candidatesTokenCount":12}}
"""


class FakeResp(io.BytesIO):
    def close(self):
        pass


captured = []


def fake_request(path, body=None, timeout=120):
    captured.append((path, body))
    return FakeResp(SSE)


b._request = fake_request
text, calls, done = [], [], None
for d in b.stream(convo, tools=tools):
    if d.kind == "text":
        text.append(d.text)
    elif d.kind == "tool_call":
        calls.append(d.tool_call)
    elif d.kind == "done":
        done = d
check("text streams in pieces", text == ["Check", "ing."], text)
check("the tool call arrives with its id",
      len(calls) == 1 and calls[0].name == "system_status"
      and calls[0].id == "call_77", calls)
check("and with its signature", calls[0].signature == "SIG-Z")
check("the signature survives into the agent's history",
      calls[0].to_openai().get("thought_signature") == "SIG-Z")
check("output tokens are reported", done and done.n_tokens == 12)
check("prompt-cache hits are tallied", b.usage["cache_read"] == 7000, b.usage)
check("streaming endpoint with SSE is used",
      captured[-1][0].endswith(":streamGenerateContent?alt=sse"), captured[-1][0])
check("the key never appears in the URL", "test-key" not in captured[-1][0])
check("ToolCall without a signature stays clean for other backends",
      "thought_signature" not in ToolCall("x").to_openai())

# ------------------------------------------------------------------ failures
print("\n=== failures ===")


def run_with(errors):
    """Raise each queued error in turn, then succeed."""
    seq = list(errors)
    seen = []

    def req(path, body=None, timeout=120):
        seen.append((path, body))
        if seq:
            raise seq.pop(0)
        return FakeResp(b'data: {"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}\n')
    g = GeminiBackend(key="k")
    g._request = req
    out = "".join(d.text for d in g.stream(convo, tools=tools) if d.kind == "text")
    return out, seen


out, seen = run_with([_HTTPError(503, "high demand"), _HTTPError(503, "high demand")])
check("503 overload is retried, then succeeds", out == "ok" and len(seen) == 3,
      (out, len(seen)))
out, seen = run_with([_HTTPError(503, "x")] * 3)
check("a model that stays overloaded hands off to a fallback model",
      out == "ok" and C.FALLBACKS[0] in seen[-1][0], [s[0] for s in seen])
out, seen = run_with([_HTTPError(404, "no longer available")])
check("a withdrawn model falls back immediately",
      out == "ok" and len(seen) == 2 and C.FALLBACKS[0] in seen[-1][0])
out, seen = run_with([_HTTPError(400, "Function call is missing a thought_signature")])
sigs = [p.get("thoughtSignature") for c in seen[-1][1]["contents"]
        for p in c["parts"] if "functionCall" in p]
check("a rejected signature is retried once with the bypass value",
      out == "ok" and sigs[0] == BYPASS_SIGNATURE, sigs)
out, seen = run_with([_HTTPError(402, "Your prepayment credits are depleted.")])
check("out of credit is not retried", len(seen) == 1, len(seen))
check("and says what to do about it",
      "credit" in out and "/backend local" in out, out)
out, seen = run_with([_HTTPError(403, "API key not valid")])
check("a bad key says so plainly", "rejected the API key" in out, out)

EMPTY = b'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"","thoughtSignature":"S"}]},"finishReason":"STOP"}]}\n'
OK = b'data: {"candidates":[{"content":{"parts":[{"text":"Calculator has been closed."}]}}]}\n'
for label, replies, want, n in (
        ("an empty reply is retried and the real answer shown",
         [EMPTY, OK], "Calculator has been closed.", 2),
        ("a second empty reply is not retried forever", [EMPTY, EMPTY, OK], "", 2)):
    q = list(replies); seen = []
    def req(path, body=None, timeout=120, q=q, seen=seen):
        seen.append(path)
        return FakeResp(q.pop(0))
    g = GeminiBackend(key="k"); g._request = req
    ds = list(g.stream(convo, tools=tools))
    out = "".join(d.text for d in ds if d.kind == "text")
    check(label, out == want and len(seen) == n, (out, len(seen)))
check("exactly one 'done' reaches the agent", [d.kind for d in ds].count("done") == 1)

print("\n=== quota: skip an exhausted model instead of retrying it ===")
QUOTA_DAY = _HTTPError(429, "You exceeded your current quota", [{"violations": [
    {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
     "quotaMetric": "x/generate_content_free_tier_requests", "quotaValue": "1500"}]}])
QUOTA_MIN = _HTTPError(429, "You exceeded your current quota", [{"violations": [
    {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
     "quotaValue": "10"}]}])
C._COOLDOWN.clear()
out, seen = run_with([QUOTA_DAY])
check("a used-up model is not retried - the next one answers straight away",
      out == "ok" and len(seen) == 2 and C.FALLBACKS[0] in seen[1][0],
      [x[0] for x in seen])
check("Google's own limit is read from the error",
      QUOTA_DAY.quota()[0]["limit"] == "1500")
# Compared with the real time to the reset, not "more than an hour": run
# just before Pacific midnight, the correct cooldown is minutes.
_left = C._next_pacific_midnight() - __import__("time").time()
check("a daily quota cools the model until the Pacific-midnight reset",
      abs(C.cooling(C.DEFAULT_MODEL) - _left) < 120, (C.cooling(C.DEFAULT_MODEL), _left))
out, seen = run_with([])
check("the next request skips the exhausted model entirely",
      out == "ok" and C.DEFAULT_MODEL not in seen[0][0], seen[0][0])
C._COOLDOWN.clear()
run_with([QUOTA_MIN])
check("a per-minute limit cools it for about a minute",
      30 < C.cooling(C.DEFAULT_MODEL) <= 60, C.cooling(C.DEFAULT_MODEL))
import time as _t
for m in (C.DEFAULT_MODEL, *C.FALLBACKS):
    C._COOLDOWN[m] = _t.time() + 600
out, seen = run_with([])
check("with every Flash model limited, a Lite model keeps JARVIS answering",
      out == "ok" and C.LAST_RESORT[0] in seen[0][0], [x[0] for x in seen])
for m in C.LAST_RESORT:
    C._COOLDOWN[m] = _t.time() + 600
out, seen = run_with([])
check("with every model used up, it says so and offers the local model",
      not seen and "/backend local" in out, out)
C._COOLDOWN.clear()

# ------------------------------------------------------------------ defaults
print("\n=== backend selection ===")
import jarvis.core.llm as L

os.environ.pop("JARVIS_BACKEND", None)
from jarvis.core.azure import AzureBackend as _AZ
want = "azure" if _AZ().available else ("cloud" if C.api_key() else "local")
check(f"default follows the order Azure > Gemini > local (here: {want})",
      L.default_kind() == want, L.default_kind())
os.environ["JARVIS_BACKEND"] = "local"
check("JARVIS_BACKEND=local still forces the local model",
      L.default_kind() == "local")
os.environ.pop("JARVIS_BACKEND")
g = GeminiBackend(key="k")
check("the default is the model the user chose (3.8 Flash)",
      g.model_id == "gemini-3.8-flash", g.model_id)
check("fallbacks are full Flash models, never Lite",
      all("flash" in m and "lite" not in m for m in C.FALLBACKS), C.FALLBACKS)
check("it satisfies the backend contract",
      all(callable(getattr(g, m, None)) for m in
          ("stream", "load", "warm", "touch_weights", "count_tokens",
           "describe_images", "complete", "unload")))

print("\n=== the local model still renders a Gemini-era history ===")
from jarvis.core import protocol as P

rendered = P.render_assistant("", [ToolCall("system_status", {}, "c1", "",
                                            "SIG").to_openai()])
check("a signed call renders for Gemma with no signature leaking into it",
      "system_status" in rendered and "SIG" not in rendered, rendered)

print(f"\n{sum(R)}/{len(R)} Gemini backend checks passed")
sys.exit(0 if all(R) else 1)
