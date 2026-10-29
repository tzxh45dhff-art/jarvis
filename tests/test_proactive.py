"""
Suggestions: right moment, fast, and restrained. Local only - the screen,
players and clock are faked so every rule can be checked deterministically.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _isolate  # noqa: F401

os.environ["JARVIS_LEARNING"] = "1"

from jarvis import proactive as S
from jarvis.memory import profile as P
import jarvis.context as ctx
import jarvis.tools.media as media

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {str(e)[:300]}"))
    R.append(bool(c))


playing = {"music": False, "video": False}
ctx.players = lambda: ([{"app": "Spotify", "state": "playing", "title": "x", "artist": "y"}]
                       if playing["music"] else [{"app": "Spotify", "state": "paused", "title": "x", "artist": "y"}])
media.browser_media_playing = lambda: playing["video"]
S._calendar_ready = lambda: False
fixed_hour = {"h": 19}
_real_localtime = time.localtime
S.time.localtime = lambda t=None: _real_localtime(t) if t is None and False else \
    time.struct_time((2026, 10, 16, fixed_hour["h"], 0, 0, 4, 289, 0))


def reset():
    P.forget(everything=True)
    with P._conn() as c:
        c.execute("DELETE FROM settings")
    S._PENDING.clear()
    P.set_setting("suggestions", "suggest")       # off by default; these tests are about it on


print("=== the right moment ===")
reset()
P.upsert("music", "Arctic Monkeys", 1, confidence=0.9)
P.upsert("movies_tv", "The Mentalist", 1, detail="Netflix", confidence=0.9)
t = time.time()
s = S.suggest({"kind": "app_activated", "app": "Spotify"})
dt_ = time.time() - t
check("opening Spotify with nothing playing offers their music", s and "Arctic Monkeys" in s["text"], s)
check("the offer carries a ready request", s and s["prompt"].startswith("play Arctic Monkeys"), s)
check("decided in well under a second", dt_ < 0.3, dt_)
s = S.suggest({"kind": "app_activated", "app": "Safari", "title": "Netflix"})
check("Netflix in front offers the show they watch there", s and "The Mentalist" in s["text"], s)

print("\n=== never redundant ===")
reset()
P.upsert("music", "Arctic Monkeys", 1, confidence=0.9)
P.upsert("movies_tv", "The Mentalist", 1, detail="Netflix", confidence=0.9)
playing["video"] = True
check("nothing is offered once a video is playing",
      S.suggest({"kind": "app_activated", "app": "Safari", "title": "Netflix"}) is None)
playing["video"] = False
playing["music"] = True
check("nothing is offered once music is playing",
      S.suggest({"kind": "app_activated", "app": "Spotify"}) is None)
playing["music"] = False
first = S.suggest({"kind": "app_activated", "app": "Spotify"})
check("a cool-down stops the same offer repeating",
      first is not None and S.suggest({"kind": "app_activated", "app": "Spotify"}) is None)
check("an app with nothing to offer gets nothing",
      S.suggest({"kind": "app_activated", "app": "Calculator"}) is None)

print("\n=== the user's say ===")
reset()
P.upsert("music", "Arctic Monkeys", 1, confidence=0.9)
s = S.suggest({"kind": "app_activated", "app": "Spotify"})
S.feedback(s["id"], "never")
st = S._key_state("music:Spotify")
st["last"] = 0
S._save_key_state("music:Spotify", st)
check("'don't suggest this again' is permanent",
      S.suggest({"kind": "app_activated", "app": "Spotify"}) is None)
reset()
P.upsert("music", "Arctic Monkeys", 1, confidence=0.9)
for _ in range(3):
    s = S.suggest({"kind": "app_activated", "app": "Music"})
    S.feedback(s["id"], "ignored")
    st = S._key_state("music:Music")
    st["last"] = 0
    S._save_key_state("music:Music", st)
check("ignored three times means a week off",
      S.suggest({"kind": "app_activated", "app": "Music"}) is None and S._key_state("music:Music").get("snooze_until", 0) > time.time())
reset()
P.upsert("music", "Arctic Monkeys", 1, confidence=0.9)
P.set_setting("suggestions", "off")
check("suggestions off means none", S.suggest({"kind": "app_activated", "app": "Spotify"}) is None)
with P._conn() as c:
    c.execute("DELETE FROM settings WHERE key='suggestions'")
check("suggestions are off unless turned on", S.mode() == "off" and
      S.suggest({"kind": "app_activated", "app": "Spotify"}) is None)
P.set_setting("suggestions", "auto")
s = S.suggest({"kind": "app_activated", "app": "Spotify"})
check("auto mode marks harmless music as do-it-yourself", s and s["auto"] is True, s)
P.set_setting("suggestions", "suggest")

print("\n=== quiet and budget ===")
reset()
P.upsert("music", "Arctic Monkeys", 1, confidence=0.9)
fixed_hour["h"] = 23
check("quiet hours: nothing at 23:00", S.suggest({"kind": "app_activated", "app": "Spotify"}) is None)
fixed_hour["h"] = 19
P.set_setting("sugg_day", time.strftime("%Y-%m-%d") + f"|{S.DAILY_BUDGET}")
check("the daily budget is respected", S.suggest({"kind": "app_activated", "app": "Spotify"}) is None)

print("\n=== morning briefing ===")
reset()
P.upsert("music", "Arctic Monkeys", 1, confidence=0.9)
P.set_setting("name", "Sam")
fixed_hour["h"] = 9
b = S.suggest({"kind": "opened"})
check("first open in the morning greets by name and offers music",
      b and b["text"].startswith("Morning, Sam!") and "Arctic Monkeys" in b["text"], b)
check("only once a day", S.suggest({"kind": "opened"}) is None)
fixed_hour["h"] = 19

print(f"\n{sum(R)}/{len(R)} suggestion checks passed")
sys.exit(0 if all(R) else 1)
