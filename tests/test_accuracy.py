"""
Tool-calling accuracy: argument repair, planning, result truncation, post-action
observation, verified music controls, downloads, paged reading, grounded vision,
and the reasoning path for hard tasks.

Fully offline: the model, network, display, music player and file downloads are
all stand-ins. Nothing on the machine is clicked, typed, played or downloaded.
"""
import io
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _isolate  # noqa: F401  (keeps tests out of the user's memory)

from jarvis.tools import REGISTRY
from jarvis.tools.registry import normalize_args

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


T = REGISTRY.get

# ------------------------------------------------------------- registration
print("=== every tool name runs the function of the same name ===")
# A helper once landed between a @REGISTRY.tool decorator and its function, so
# "see_screen" silently ran the helper and failed on every call.
wrong = [n for n in REGISTRY.names()
         if not REGISTRY.get(n).fn.__name__.startswith(n)]
check("no tool is bound to the wrong function", not wrong, wrong)

# ------------------------------------------------------------- arguments
print("=== malformed calls are repaired or refused precisely ===")
cases = [
    ("press_key", {"key": "cmd+s", "app_name": "TextEdit"},
     {"keys": "cmd+s", "app": "TextEdit"}),
    ("set_volume", {"level": "50"}, {"level": 50}),
    ("play_music", {"song": "Yellow", "app": "spotify"},
     {"query": "Yellow", "app": "Spotify"}),
    ("quit_apps", {"names": "Safari"}, {"names": ["Safari"]}),
    ("fetch_many", {"urls": '["https://a.com", "https://b.com"]'},
     {"urls": ["https://a.com", "https://b.com"]}),
    ("set_wifi", {"on": "yes"}, {"on": True}),
    ("download_file", {"link": "https://x.com/a.pdf"}, {"url": "https://x.com/a.pdf"}),
]
for tool, given, want in cases:
    got, notes = normalize_args(T(tool), given)
    check(f"{tool} {given} -> {want}", got == want, (got, notes))
for tool, given, why in [("set_volume", {"level": "loud"}, "whole number"),
                         ("open_app", {"colour": "red", "name": "Notes"}, "no argument 'colour'"),
                         ("web_search", {"query": "  "}, "missing required"),
                         ("control_playback", {"action": "fast-forward"}, "must be one of")]:
    try:
        normalize_args(T(tool), given)
        check(f"{tool} {given} refused", False)
    except ValueError as e:
        check(f"{tool} {given} refused ({why})", why in str(e), str(e))

asked = []
REGISTRY.confirm_hook = lambda n, a, v: asked.append(n) or False
r = REGISTRY.execute("quit_apps", {"nmes": 5, "bogus": 1})
check("an invalid call is refused before the user is asked to confirm",
      not r.ok and not asked and "Nothing was done" in r.error, (r.error, asked))
REGISTRY.confirm_hook = None
r = REGISTRY.execute("click_button", {})
check("an invented tool name gets a 'did you mean'", "Did you mean" in r.error
      and "click" in r.error and len(r.error) < 200, r.error)

# ------------------------------------------------------------- truncation
print("\n=== long results stay valid JSON and say what was cut ===")
from jarvis.core.agent import _for_model

big = {"title": "Page", "content": "x" * 50000, "items": list(range(5000))}
s = _for_model(big, 12000)
check("fits the budget", len(s) <= 12000, len(s))
try:
    d = json.loads(s)
    check("is still valid JSON", True)
    check("keeps the short fields intact", d["title"] == "Page")
    check("marks how much text was cut", "chars cut" in d["content"])
    check("marks how many list items were cut", any("more items cut" in str(x) for x in d["items"]))
except ValueError as e:
    check("is still valid JSON", False, e)
check("small results pass through unchanged", _for_model({"a": 1}) == '{"a": 1}')

# ------------------------------------------------------------- planning
print("\n=== multi-step requests get a plan first ===")
from jarvis.core.agent import Agent


def D(kind, text="", tool_call=None):
    return types.SimpleNamespace(kind=kind, text=text, tool_call=tool_call,
                                 n_tokens=1, tps=1, prompt_tps=1)


class PlanLLM:
    _kind = "azure"

    def __init__(self):
        self.seen = []

    def stream(self, messages, tools=None, think=False, max_tokens=None,
               temperature=None, stop_event=None):
        last = messages[-1]["content"]
        self.seen.append((tools is not None, think, last[:60]))
        if "Begin your reply with a short numbered plan" in last:
            # The plan arrives in the same reply as the first actions.
            yield D("text", "Here is the plan:\n1. web_search for laptops\n"
                            "2. download_file the spec sheet\n3. play_music lofi\n"
                            "4. confirm all three")
        else:
            yield D("text", "CONFIRMED" if "Before answering" in last else "All done.")
        yield D("done")


llm = PlanLLM()
ag = Agent(llm)
ag._relevant_memories = lambda t: ""
ag._auto_capture = lambda a, b: None
task = "search for the best laptop, download its spec sheet and then play some lofi"
evs = list(ag.run(task, max_steps=3))
plans = [e.text for e in evs if e.type == "plan"]
check("a plan is made and shown", plans and plans[0].count("\n") == 3, plans)
check("only the numbered steps are kept", plans and "Here is the plan" not in plans[0])
user_turn = next(m for m in ag.messages if m["role"] == "user")
check("the plan is asked for in the same request, not a separate call",
      "numbered plan" in user_turn["content"] and len(llm.seen) >= 2
      and not any("Plan how to carry out" in x for _, _, x in llm.seen), llm.seen)
check("a plan without actions is carried on, not returned as the answer",
      any(m["role"] == "user" and "carry it out" in m["content"] for m in ag.messages))
check("hard tasks keep reasoning on for every step on remote models",
      any(think for tools, think, _ in llm.seen if tools), llm.seen)
ag2 = Agent(PlanLLM())
ag2._relevant_memories = lambda t: ""
check("a simple question gets no plan", not ag2._wants_plan("what's my battery at?"))
check("a single action gets no plan", not ag2._wants_plan("play some lofi"))

# ------------------------------------------------------------- observation
print("\n=== mouse and keyboard actions report the screen afterwards ===")
from jarvis.tools import control as C

Q = C._q()
real = (Q.CGEventPost, C.input_allowed, C.observe, C._glide, C.frontmost_app, C.focus_app)
Q.CGEventPost = lambda tap, ev: None
C.input_allowed = lambda: True
C._glide = lambda x, y, *a, **k: None
C.observe = lambda settle=0.25: {"front_app": "Safari", "window": "Docs",
                                 "dialog": "sheet: Save / Cancel"}
r = C.click(100, 100)
check("a click reports the app, window and any dialog it produced",
      r.ok and r.content["screen_after"]["dialog"].startswith("sheet"), r.content)
r = C.click(99999, 99999)
check("a click off every display is refused, nothing clicked",
      not r.ok and "off-screen" in r.error, r.error)
C.frontmost_app = lambda fresh=False: "TextEdit"
C.focus_app = lambda app, timeout=3.0: (True, "TextEdit")
r = C.press_key("cmd+s", app="TextEdit")
check("a key press reports where it landed", r.ok and r.content.get("screen_after"), r.content)
r = C.press_key("cmd+a", app="TextEdit", quiet=True)
check("internal key presses skip the check (no extra delay)",
      r.ok and r.content.get("screen_after") == {}, r.content)
Q.CGEventPost, C.input_allowed, C.observe, C._glide, C.frontmost_app, C.focus_app = real

# ------------------------------------------------------------- music
print("\n=== playback controls are confirmed, not assumed ===")
from jarvis.tools import media as M

real_m = (M._now_playing, M.osa.run, M.osa.app_installed, M.osa.app_running)
M.osa.app_installed = lambda a: True
M.osa.app_running = lambda a: True
M.osa.run = lambda *a, **k: types.SimpleNamespace(ok=True, out="50", error="")
state = {"v": {"title": "Song A", "artist": "X", "state": "playing"}}
M._now_playing = lambda app="Music": state["v"]
r = M.control_playback("pause", app="Music")
check("'pause' that did nothing is a failure", not r.ok and "still playing" in r.error, r)
state["v"] = {"title": "Song A", "artist": "X", "state": "paused"}
r = M.control_playback("pause", app="Music")
check("'pause' that worked is confirmed", r.ok and r.content["verified"], r)
state["v"] = {"title": "Song A", "artist": "X", "state": "playing"}
r = M.control_playback("next", app="Music")
check("'next' with the same track still playing is a failure",
      not r.ok and "did not change" in r.error, r)

vol = {"v": "30"}
M.osa.run = lambda script, *a, **k: types.SimpleNamespace(
    ok=True, out=vol["v"], error="")
r = M.set_volume(level=80)
check("a volume change the device ignored is reported", not r.ok and "80" in r.error, r)
vol["v"] = "81"
r = M.set_volume(level=80)
check("a volume change within macOS's steps is confirmed", r.ok and r.content["verified"], r)

print("\n=== the right song, not just the right album ===")
T_ = lambda t, a="Coldplay", al="Viva La Vida or Death and All His Friends": {
    "title": t, "artist": a, "album": al}
for track, want, exp in [
        (T_("Life in Technicolor"), "Viva la Vida", False),   # album match only
        (T_("Viva La Vida"), "Viva la Vida by Coldplay", True),
        (T_("Fix You", al="X&Y"), "Yellow", False),
        (T_("Blinding Lights", a="The Weeknd"), "blinding lights the weeknd", True),
        (T_("Yellow"), "Coldplay", True), (T_("Yellow"), "Adele", False)]:
    check(f"{want!r} vs playing {track['title']!r} -> {exp}", M._matches(track, want) == exp)

print("\n=== catalogue picks the original recording, and never skips tracks ===")
RESULTS = {"results": [
    {"trackName": "Yellow (Coldplay) [Fingerstyle]", "artistName": "Daniel Padim",
     "collectionName": "Single", "trackViewUrl": "https://m/1"},
    {"trackName": "Yellow - Karaoke Version", "artistName": "Karaoke Hits",
     "collectionName": "K", "trackViewUrl": "https://m/2"},
    {"trackName": "Yellow", "artistName": "Coldplay",
     "collectionName": "Parachutes", "trackViewUrl": "https://m/3"}]}
real_open = M.urllib.request.urlopen
M.urllib.request.urlopen = lambda url, timeout=0: io.BytesIO(json.dumps(RESULTS).encode())
hit = M._catalog_lookup("Yellow Coldplay")
M.urllib.request.urlopen = real_open
check("the artist's own recording beats a cover with their name in the title",
      hit and hit["artist"] == "Coldplay" and hit["url"] == "https://m/3", hit)
import inspect
src = inspect.getsource(M._play_on_music)        # the Apple Music catalogue path
check("the catalogue path never steps through the album with 'next track'",
      "next track" not in src and "_play_row" in src)

print("\n=== YouTube fallback says whether sound actually started ===")
real_yt = (M._youtube_top, M.browser_media_ids, M.subprocess.run)
M._youtube_top = lambda q: {"id": "abcdefghijk", "title": "Lofi Beats"}
M.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="")
# another tab already playing (id A); the new video adds id B
seq = iter([{"0xA"}, {"0xA"}, {"0xA", "0xB"}])
M.browser_media_ids = lambda: next(seq, {"0xA", "0xB"})
r = M.play_music("lofi beats", app="YouTube")
check("a new playback appearing -> verified, even with another tab playing",
      r.ok and r.content["verified"] is True, r.content)
M.browser_media_ids = lambda: {"0xA"}      # only the old, lingering one
M.time.sleep = lambda s: None
real_time = M.time.time
clock = {"t": 0}
M.time.time = lambda: clock.__setitem__("t", clock["t"] + 1) or clock["t"]
r = M.play_music("lofi beats", app="YouTube")
check("no sound -> opened but NOT claimed as playing",
      r.ok and r.content["verified"] is False and "could not confirm" in r.content["note"], r.content)
M.time.time = real_time
M.time.sleep = time.sleep
M._youtube_top, M.browser_media_ids, M.subprocess.run = real_yt
M._now_playing, M.osa.run, M.osa.app_installed, M.osa.app_running = real_m
check("the playback check reads macOS power assertions",
      isinstance(M.browser_media_ids(), (set, type(None))))

# ------------------------------------------------------------- downloads
print("\n=== downloads end with a checked file on disk ===")
from jarvis.tools import downloads as DL
from jarvis.safety.policy import needs_confirmation


class FakeResp:
    def __init__(self, body, ctype="application/pdf", status=200, length=True, url=""):
        self.body, self.status_code, self.url = body, status, url
        self.headers = {"Content-Type": ctype}
        if length:
            self.headers["Content-Length"] = str(len(body))

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]

    def close(self):
        pass


import requests

real_get, real_rec, real_q = requests.get, DL.journal.record, DL._quarantine
recorded, quarantined = [], []
DL.journal.record = lambda op, path, **k: recorded.append((op, path))
DL._quarantine = lambda path, url: quarantined.append(str(path))
with tempfile.TemporaryDirectory() as td:
    requests.get = lambda *a, **k: FakeResp(b"%PDF-1.7 hello" * 100)
    r = DL.download_file("https://example.com/files/spec.pdf", folder=td)
    p = Path(td) / "spec.pdf"
    check("a download lands under its own name, complete", r.ok and p.exists()
          and p.stat().st_size == 1400, r)
    check("it is recorded for undo", recorded and recorded[-1][1] == str(p))
    check("no .part file is left behind", not list(Path(td).glob("*.part")))
    r = DL.download_file("https://example.com/files/spec.pdf", folder=td)
    check("a second copy does not overwrite the first",
          r.ok and r.content["path"].endswith("spec (2).pdf"), r.content)
    requests.get = lambda *a, **k: FakeResp(b"<!DOCTYPE html><html>Sign in</html>",
                                            ctype="text/html")
    r = DL.download_file("https://example.com/report.pdf", folder=td)
    check("a login page posing as a PDF is caught, nothing kept",
          not r.ok and "web page" in r.error and not (Path(td) / "report.pdf").exists(), r)
    fr = FakeResp(b"x" * 10)
    fr.headers["Content-Length"] = "100"
    requests.get = lambda *a, **k: fr
    r = DL.download_file("https://example.com/big.zip", folder=td)
    check("an incomplete download is refused", not r.ok and "incomplete" in r.error, r)
    requests.get = lambda *a, **k: FakeResp(b"\x00" * 64, ctype="application/octet-stream")
    r = DL.download_file("https://example.com/Tool.dmg", folder=td)
    check("installers keep macOS's quarantine flag", r.ok and quarantined, quarantined)
    requests.get = lambda *a, **k: FakeResp(b"", status=403)
    r = DL.download_file("https://example.com/private.pdf", folder=td)
    check("a 403 explains that a login is probably needed", not r.ok and "login" in r.error)
requests.get, DL.journal.record, DL._quarantine = real_get, real_rec, real_q
check("downloading a program asks first",
      needs_confirmation(REGISTRY.assess("download_file", {"url": "https://x.com/a.pkg"})))
check("downloading a document does not",
      not needs_confirmation(REGISTRY.assess("download_file", {"url": "https://x.com/a.pdf"})))

with tempfile.TemporaryDirectory() as td:
    (Path(td) / "movie.mp4.crdownload").write_bytes(b"x")
    r = DL.wait_for_download(folder=td, timeout=5, _since=time.time() - 5)
    check("a half-finished browser download is not reported as done",
          not r.ok and "still downloading" in r.error, r.error)
    (Path(td) / "movie.mp4.crdownload").unlink()
    (Path(td) / "paper.pdf").write_bytes(b"%PDF" * 50)
    r = DL.wait_for_download(folder=td, timeout=8, _since=time.time() - 5)
    check("a finished download is found and reported",
          r.ok and r.content["path"].endswith("paper.pdf"), r)

# ------------------------------------------------------------- web reading
print("\n=== long pages are read in parts, JS pages are flagged ===")
from jarvis.tools import web as W

real_f = W._fetch_one
W._fetch_one = lambda url, n: {"url": url, "ok": True, "title": "T", "chars": 25000,
                              "content": "a" * 25000}
r = W.fetch_page("https://x.com")
check("first part comes with a pointer to the rest",
      r.ok and r.content["more"] and r.content["next_offset"] == 9000, r.content.get("note"))
r = W.fetch_page("https://x.com", offset=18000)
check("the last part says there is nothing more", r.ok and not r.content.get("more")
      and len(r.content["content"]) == 7000)
W._fetch_one = lambda url, n: {"url": url, "ok": True, "title": "App", "chars": 40,
                              "content": "Loading..."}
r = W.fetch_page("https://app.example.com")
check("a near-empty page is flagged as needing the browser",
      "browser_open" in r.content.get("warning", ""), r.content)
W._fetch_one = real_f

# ------------------------------------------------------------- vision
print("\n=== screen vision is grounded in exact facts ===")
from jarvis.tools import screen as S
import jarvis.core.llm as L

real_s = (S.take_screenshot, S._ground, L.get_backend)
S.take_screenshot = lambda reg=None, detail=False: Path("/tmp/fake.png")
S._ground = lambda reg: ({"front_app": "Safari", "window": "Login",
                          "dialog": "sheet: Allow notifications?"},
                         [{"text": "Sign in", "x": 400, "y": 300}])
prompts_seen = []
L.get_backend = lambda *a: types.SimpleNamespace(
    describe_images=lambda p, imgs, max_tokens=600: prompts_seen.append(p) or "A login page.")
r = S.see_screen("what is on screen?")
check("the vision model is told the front app, dialog and exact text",
      prompts_seen and "Safari" in prompts_seen[0] and "Allow notifications" in prompts_seen[0]
      and "Sign in" in prompts_seen[0], prompts_seen)
check("it is told not to estimate coordinates", "Do not estimate" in prompts_seen[0])
check("the result carries text with real click positions",
      r.ok and r.content["text_on_screen"][0] == {"text": "Sign in", "x": 400, "y": 300})

# Reading the screen must not cost a vision-model round trip unless the
# question is really about how something looks (3.7 s of a 3.7 s call).
rich = [{"text": f"Item {i}", "x": 10 * i, "y": 20} for i in range(8)]
S._ground = lambda reg: ({"front_app": "Finder", "window": "Downloads"}, rich)
vlm_calls = []
L.get_backend = lambda *a: types.SimpleNamespace(
    describe_images=lambda p, imgs, max_tokens=600: vlm_calls.append(p) or "described")
r = S.see_screen("which file is at the top of the list?")
check("a text question is answered without the vision model",
      r.ok and not vlm_calls and r.content["mode"] == "text", (vlm_calls, r.content))
check("and still lists the app, window and clickable text",
      "Finder" in r.content["observation"] and "Downloads" in r.content["observation"]
      and r.content["text_on_screen"][0]["text"] == "Item 0")
r = S.see_screen("what colour is the icon in the dock?")
check("a question about appearance uses the vision model",
      r.ok and len(vlm_calls) == 1 and r.content["mode"] == "vision", vlm_calls)
r = S.see_screen("what is selected?", vision=True)
check("vision=true forces it", r.ok and len(vlm_calls) == 2)
S._ground = lambda reg: ({"front_app": "Preview"}, [{"text": "x", "x": 1, "y": 1}])
r = S.see_screen("what is on the page?")
check("a mostly-graphic screen (little text) falls back to vision",
      r.ok and len(vlm_calls) == 3, vlm_calls)
S.take_screenshot, S._ground, L.get_backend = real_s

# ------------------------------------------------------------- settle time
print("\n=== actions are observed without a flat wait ===")
from jarvis.tools import control as C_
import time as _t
seq = iter([{"front_app": "A"}, {"front_app": "A"}, {"front_app": "A"}])
real_snap = C_._snapshot
C_._snapshot = lambda: next(seq)
t0 = _t.time()
res = C_.observe(0.25)
dt = _t.time() - t0
check("a settled screen returns well before the old flat 250 ms",
      res == {"front_app": "A"} and dt < 0.2, dt)
import itertools as _it
_n = _it.count()
C_._snapshot = lambda: {"front_app": str(next(_n))}      # never settles
t0 = _t.time()
res = C_.observe(0.25)
check("a screen still changing is waited on, but never past the ceiling",
      _t.time() - t0 < 0.45 and "front_app" in res, _t.time() - t0)
C_._snapshot = real_snap

# ------------------------------------------------------------- reasoning path
print("\n=== hard tasks on Azure reason through the Responses API ===")
import jarvis.core.azure as A

A._TOKEN.get = lambda: "tok"
b = A.AzureBackend(endpoint="https://e.openai.azure.com/", deployment="gpt-6-luna")
SSE = b"""data: {"type":"response.output_text.delta","delta":"Checking"}

data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"call_9","name":"web_search","arguments":"{\\"query\\":\\"x\\"}"}}

data: {"type":"response.completed","response":{"usage":{"input_tokens":50,"output_tokens":9,"input_tokens_details":{"cached_tokens":40}}}}
"""
sent = []


class SR(io.BytesIO):
    def close(self):
        pass


real_open = A.http_pool.urlopen
A.http_pool.urlopen = lambda req, timeout=0: (sent.append(json.loads(req.data)) or SR(SSE))
msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "list_dir", "arguments": {}}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "list_dir", "content": "{}"}]
tools = [{"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}}]
ds = list(b.stream(msgs, tools=tools, think=True))
calls = [d.tool_call for d in ds if d.kind == "tool_call"]
check("a reasoning turn goes to the Responses API with reasoning on",
      sent and sent[-1].get("reasoning") == {"effort": "low"} and "input" in sent[-1])
check("tool history is translated into function_call items",
      any(i.get("type") == "function_call_output" for i in sent[-1]["input"]))
check("its tool call comes back intact",
      calls and calls[0].name == "web_search" and calls[0].arguments == {"query": "x"}
      and calls[0].id == "call_9", [(c.name, c.arguments) for c in calls])


def broken(req, timeout=0):
    body = json.loads(req.data)
    sent.append(body)
    if "input" in body:
        raise OSError("responses API unavailable")
    return SR(b'data: {"choices":[{"delta":{"content":"fallback ok"}}]}\n\ndata: [DONE]\n')


A.http_pool.urlopen = broken
b._post = lambda body, stream=True: broken(types.SimpleNamespace(data=json.dumps(body).encode()))
out = "".join(d.text for d in b.stream(msgs, tools=tools, think=True) if d.kind == "text")
check("if the Responses API fails, the fast path answers instead", out == "fallback ok", out)
A.http_pool.urlopen = real_open

# ------------------------------------------------------------- prompt
print("\n=== the guide points at the right tools ===")
from jarvis.core import prompts

sp = prompts.system_prompt()
check("music goes through play_music, not a hand-opened site",
      "Never open a streaming site by hand" in sp and "open its URL; to message" not in sp)
check("downloads have a rule", "download_file" in sp and "wait_for_download" in sp)
check("unconfirmed results must not be reported as done", "`verified: false`" in sp)
check("controls by name rank above screen-text clicking",
      sp.index("press_element") < sp.index("click_text"))

# ------------------------------------------------------------- tabs, voice
print("\n=== browser_open reuses JARVIS's own tab ===")
from jarvis.tools import browser as BR

calls = []
real_run = BR.osa.run
BR.osa.run = lambda script, *a, **k: (calls.append(a), types.SimpleNamespace(
    ok=True, out="4200|2|new" if not calls[:-1] else "4200|2|reused", error=""))[1]
BR._OWNED.clear()
check("first open makes a tab", BR._open_in_owned_tab("Safari", "https://leetcode.com/a") == "new")
check("it remembers which tab, window and site",
      BR._OWNED["Safari"] == {"win": "4200", "tab": "2", "host": "leetcode.com"})
check("the next open hands the remembered tab and site back to the script",
      BR._open_in_owned_tab("Safari", "https://leetcode.com/b") == "reused"
      and calls[-1][1:] == ("4200", "2", "leetcode.com"), calls[-1])
check("an unscriptable browser falls back to the old behaviour",
      BR._open_in_owned_tab("Arc", "https://x.com") is None)
BR.osa.run = real_run
check("the script only reuses a tab still on the site JARVIS left it on",
      "contains lastHost" in BR._SAFARI_OPEN and "contains lastHost" in BR._CHROME_OPEN)

print("\n=== voice models are not loaded unless enabled ===")
from jarvis.config import CONFIG as CFG
import inspect
from jarvis.daemon import server as SRV
check("voice is off by default", CFG.voice.enabled is False or
      os.environ.get("JARVIS_VOICE_ENABLED"), CFG.voice.enabled)
src = inspect.getsource(SRV.Daemon._load)
check("daemon start skips the speech warm-up when voice is off",
      "CONFIG.voice.enabled" in src and src.index("CONFIG.voice.enabled")
      < src.index("WhisperSTT"))
check("hold-to-talk is refused politely instead of loading Whisper",
      "turned off for now" in inspect.getsource(SRV.Daemon._listen))

print(f"\n{sum(R)}/{len(R)} accuracy checks passed")
sys.exit(0 if all(R) else 1)
