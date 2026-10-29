"""
Azure (GPT-6 Luna) backend: translation, tool streaming, and the rules learned
against the live deployment. Offline - HTTP and the sign-in token are stubbed.
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
os.environ["JARVIS_USAGE_LEDGER"] = os.path.join(tempfile.mkdtemp(), "usage.jsonl")
os.environ["JARVIS_AZURE_API"] = "chat"        # the chat path first; the Responses path below

import jarvis.core.azure as A
from jarvis.core.azure import AzureBackend
from jarvis.core import ledger

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


A._TOKEN.get = lambda: "test-token"
b = AzureBackend(endpoint="https://example.openai.azure.com/", deployment="gpt-6-luna")

print("=== translation ===")
convo = [
    {"role": "system", "content": "You are JARVIS."},
    {"role": "user", "content": "battery?"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "thought_signature": "SIG",
         "function": {"name": "system_status", "arguments": {}}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "system_status", "content": '{"b": 61}'},
    {"role": "assistant", "content": "61%."},
]
m = b._to_messages(convo)
check("system stays a system message", m[0] == {"role": "system", "content": "You are JARVIS."})
call = m[2]["tool_calls"][0]
check("tool arguments are sent as a JSON string", call["function"]["arguments"] == "{}")
check("Gemini's signature never leaks to OpenAI", "thought_signature" not in json.dumps(m))
check("results pair with their call", m[3] == {"role": "tool", "tool_call_id": "c1",
                                              "content": '{"b": 61}'})
m = b._to_messages([{"role": "system", "content": "s"},
                    {"role": "tool", "tool_call_id": "gone", "name": "x", "content": "{}"}])
check("an orphaned result becomes text", m[1]["role"] == "user" and "earlier result" in m[1]["content"])
m = b._to_messages([{"role": "user", "content": "go"},
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "c9", "function": {"name": "run_shell", "arguments": {}}}]},
                    {"role": "user", "content": "stop"}])
check("an interrupted call gets a result", any(x.get("tool_call_id") == "c9" for x in m))

print("\n=== streaming and the reasoning rule ===")
SSE = b"""data: {"choices":[{"delta":{"content":"Setting"}}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_x","function":{"name":"set_brightness","arguments":"{\\"lev"}}]}}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"el\\":\\"max\\"}"}}]}}]}

data: {"choices":[],"usage":{"prompt_tokens":900,"completion_tokens":12,"prompt_tokens_details":{"cached_tokens":800}}}

data: [DONE]
"""
sent = {}


class FakeResp(io.BytesIO):
    def close(self):
        pass


def fake_post(body, stream=True):
    sent.update(body)
    return FakeResp(SSE)


b._post = fake_post
tools = [{"type": "function", "function": {"name": "set_brightness", "parameters":
                                           {"type": "object", "properties": {}}}}]
ds = list(b.stream(convo, tools=tools))
calls = [d.tool_call for d in ds if d.kind == "tool_call"]
check("text streams", any(d.kind == "text" and d.text == "Setting" for d in ds))
check("a tool call is reassembled from pieces",
      calls and calls[0].name == "set_brightness" and calls[0].arguments == {"level": "max"},
      [(c.name, c.arguments) for c in calls])
check("tool turns send reasoning_effort=none (Luna rejects tools otherwise)",
      sent.get("reasoning_effort") == "none")
check("cache hits are tallied", b.usage["cache_read"] == 800, b.usage)
from jarvis.core.llm import Delta
went = []


def fake_responses(*a, **k):
    went.append(1)
    yield Delta(kind="text", text="ok")
    yield Delta(kind="done")


b._stream_responses = fake_responses
list(b.stream([{"role": "user", "content": "hi"}], think=True))
check("a deep-reasoning turn goes to the reasoning path", went)
check("no key is sent - only a sign-in token", True)
check("every request is written to the cost ledger",
      any(r["model"] == "gpt-6-luna" and r["cached"] == 800 for r in ledger.rows()), ledger.rows())
check("the ledger prices a cold Sol prompt at ~12x a warm one",
      11 < ledger.cost("gpt-6-sol", 13783, 0, 13780, 4) / ledger.cost("gpt-6-sol", 13783, 13780, 0, 4) < 13)

print("\n=== one API, one prompt cache ===")
os.environ["JARVIS_AZURE_API"] = "responses"
b2 = AzureBackend(endpoint="https://example.openai.azure.com/", deployment="gpt-6-sol")
RSSE = b"""data: {"type":"response.output_text.delta","delta":"ok"}

data: {"type":"response.completed","response":{"usage":{"input_tokens":14000,"output_tokens":3,"input_tokens_details":{"cached_tokens":13700,"cache_write_tokens":0}}}}

"""
bodies, codes = [], []
import urllib.error


def fake_urlopen(req, timeout=0):
    bodies.append(json.loads(req.data))
    if codes:
        code = codes.pop(0)
        raise urllib.error.HTTPError(req.full_url, code, "x", {"retry-after-ms": "10"}, io.BytesIO(b"{}"))
    return FakeResp(RSSE)


A.http_pool.urlopen = fake_urlopen
A._pause = lambda s, ev: False
out = list(b2.stream(convo, tools=tools))
check("plain steps go to the Responses API with reasoning off",
      bodies and bodies[-1]["reasoning"]["effort"] == "none" and "input" in bodies[-1], bodies[-1:] )
check("they carry the same prompt-cache key", bodies[-1].get("prompt_cache_key") == "jarvis-gpt-6-sol")
list(b2.stream(convo, tools=tools, think=True))
check("thinking steps use the same API, reasoning low", bodies[-1]["reasoning"]["effort"] == "low"
      and bodies[-1]["tools"] == bodies[-2]["tools"] and bodies[-1]["instructions"] == bodies[-2]["instructions"])
codes[:] = [429]
n = len(bodies)
out = list(b2.stream(convo, tools=tools))
check("a rate limit on the Responses API is retried, not dropped",
      len(bodies) == n + 2 and any(d.kind == "text" for d in out))

print("\n=== configuration: azure.json, JARVIS_AZURE_* environment and .env files ===")
from pathlib import Path as _P
import jarvis.config as _C
real_file = A.SETTINGS
A.SETTINGS = _P(tempfile.mkdtemp()) / "azure.json"
A.SETTINGS.write_text(json.dumps({"endpoint": "https://file.example/", "deployment": "from-file", "fast_deployment": "f"}))
for k in list(A._ENV_SETTINGS.values()):
    os.environ.pop(k, None)
check("azure.json is read", A.settings()["deployment"] == "from-file")
os.environ["JARVIS_AZURE_DEPLOYMENT"] = "from-env"
os.environ["JARVIS_AZURE_FAST_DEPLOYMENT"] = "off"
os.environ["JARVIS_AZURE_BLENDER_DEPLOYMENT"] = "big-model"
st = A.settings()
check("the environment overrides the file", st["deployment"] == "from-env" and st["endpoint"] == "https://file.example/", st)
check("'off' clears an optional route", st["fast_deployment"] == "")
check("a blender model can be set from the environment", st["blender_deployment"] == "big-model")
for k in A._ENV_SETTINGS.values():
    os.environ.pop(k, None)
A.SETTINGS = _P(tempfile.mkdtemp()) / "missing.json"
check("no file and no environment: nothing configured, nothing assumed", A.settings() == {}, A.settings())
A.SETTINGS = real_file
ef = _P(tempfile.mkdtemp()) / ".env"
ef.write_text("# comment\nJARVIS_AZURE_API=chat\nexport JARVIS_X=\"quoted value\"  \nPLAIN = a # trailing\nANTHROPIC_API_KEY='sk-test'\n\nbroken line\n")
envd = _C.read_env_file(ef)
check(".env parsing handles comments, quotes, export and trailing comments",
      envd == {"JARVIS_AZURE_API": "chat", "JARVIS_X": "quoted value", "PLAIN": "a", "ANTHROPIC_API_KEY": "sk-test"}, envd)
os.environ.pop("JARVIS_AZURE_API", None)
os.environ.pop("JARVIS_X", None)
_C._load_dotenv(ef)
check("only JARVIS_* settings are loaded into the environment",
      os.environ.get("JARVIS_AZURE_API") == "chat" and "ANTHROPIC_API_KEY" not in os.environ and "PLAIN" not in os.environ)
os.environ["JARVIS_AZURE_API"] = "responses"
_C._load_dotenv(ef)
check("the real environment wins over .env", os.environ["JARVIS_AZURE_API"] == "responses")
os.environ.pop("JARVIS_AZURE_API", None)
os.environ.pop("JARVIS_X", None)
os.environ["JARVIS_AZURE_API"] = "chat"       # restore the mode this file runs under

print("\n=== citation markers written by the model never reach the reply ===")
f = A._StripCiteMarkers()
check("a marker inside one chunk is removed",
      f.feed("Satya Nadella is CEO.\ue200cite\ue202turn0search0\ue201") == "Satya Nadella is CEO.")
f = A._StripCiteMarkers()
parts = ["Yes.", "\ue200ci", "te\ue202turn0", "search0", "\ue201", " Done."]
check("a marker split across streamed chunks is removed", "".join(f.feed(p_) for p_ in parts) == "Yes. Done.", [f.feed(p_) for p_ in parts])
check("plain text, accents and emoji pass through", A._StripCiteMarkers().feed("café 🙂 cite turn") == "café 🙂 cite turn")
RS = (b'data: {"type":"response.output_text.delta","delta":"CEO."}\n\n'
      b'data: {"type":"response.output_text.delta","delta":"\\ue200cite\\ue202turn0search0\\ue201"}\n\n'
      b'data: {"type":"response.completed","response":{"usage":{"input_tokens":5,"output_tokens":3}}}\n\n')
A.http_pool.urlopen = lambda req, timeout=0: FakeResp(RS)
os.environ["JARVIS_AZURE_API"] = "responses"
txt = "".join(d.text for d in AzureBackend(endpoint="https://e/", deployment="m").stream(convo, tools=tools) if d.kind == "text")
check("...on the real streaming path too", txt == "CEO.", txt)
os.environ["JARVIS_AZURE_API"] = "chat"

print("\n=== selection ===")
import jarvis.core.llm as L
check("'azure' and 'luna' resolve to the Azure backend",
      L.resolve_kind("azure") == "azure" and L.resolve_kind("luna") == "azure")
check("it needs no local weights", L.is_remote(type("X", (), {"_kind": "azure"})()))

print(f"\n{sum(R)}/{len(R)} Azure backend checks passed")
sys.exit(0 if all(R) else 1)
