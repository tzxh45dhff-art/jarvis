"""
The learned profile: facts merge and strengthen, tastes come from what the
user does as well as says, sensitive things are never stored, and forgetting
works. Local only (a throwaway database), except the last section, which asks
the real model to pull facts out of sample sentences (set JARVIS_LIVE=0 to skip).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _isolate  # noqa: F401

os.environ["JARVIS_LEARNING"] = "1"           # this suite is about learning

from jarvis.memory import profile as P

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {str(e)[:300]}"))
    R.append(bool(c))


def items(cat=None, pol=None):
    return [f["item"] for f in P.facts(cat) if pol is None or f["polarity"] == pol]


print("=== facts ===")
P.upsert("music", "Arctic Monkeys", 1, confidence=0.6)
c1 = P.facts("music")[0]["confidence"]
P.upsert("music", "arctic monkeys", 1, confidence=0.6)
f = P.facts("music")[0]
check("a repeated fact merges (case-insensitive) and grows surer", len(items("music")) == 1 and f["confidence"] > c1 and f["hits"] == 2, f)
P.upsert("food", "Pineapple pizza", 1)
P.upsert("food", "Pineapple pizza", -1, source="said")
check("a change of heart replaces the old feeling", P.facts("food")[0]["polarity"] == -1)
check("passwords are never stored", not P.upsert("other", "bank password is hunter2"))
check("health is never stored", not P.upsert("other", "takes medication for anxiety"))
check("an unknown category files under other", P.upsert("weird", "Collects stamps") and "Collects stamps" in items("other"))

print("\n=== learning from what they do ===")
P.observe("music_played", "Hozier", "Like Real People Do")
P.observe("music_played", "Hozier", "Work Song")
check("two plays seen in passing are not yet a taste", "Hozier" not in items("music"))
P.observe("music_played", "Hozier", "Cherry Wine")
check("the third play is", "Hozier" in items("music"))
P.observe_tool("play_music", {"query": "blinding lights"},
               {"title": "Blinding Lights", "artist": "The Weeknd", "app": "Spotify", "verified": True})
check("asking JARVIS to play an artist counts at once", "The Weeknd" in items("music"))
P.observe_tool("browser_open", {"url": "https://www.netflix.com/search?q=the%20mentalist"}, {"verified": True})
check("opening a show on Netflix records the show", "The Mentalist" in items("movies_tv"), items("movies_tv"))
check("…and the platform", "Netflix" in items("streaming"), items("streaming"))

print("\n=== routines ===")
import sqlite3
now = time.time()
hr = time.localtime().tm_hour
wd = time.localtime().tm_wday
with sqlite3.connect(P.DB) as c:
    added = 0
    for d in range(1, 15):         # three earlier days of the same kind (weekday/weekend), same hour
        day_wd = time.localtime(now - d * 86400).tm_wday
        if (day_wd >= 5) != (wd >= 5) or added >= 3:
            continue
        c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (now - d * 86400, "app_used", "Xcode", "", hr, day_wd))
        added += 1
    c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (now - 86400, "app_used", "Chess", "", (hr + 6) % 24, wd))
r = P.routine()
check("an app used at this hour on several days is a routine", any(s == "Xcode" for s, _ in r["apps"]), r)
check("an app used once at another hour is not", all(s != "Chess" for s, _ in r["apps"]), r)

print("\n=== taste that evolves ===")
import sqlite3 as _sq
now = time.time()
with _sq.connect(P.DB) as c:
    for d in range(20, 55, 3):                       # an old favourite, steady
        c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (now - d * 86400, "music_played", "Coldplay", "", 20, 2))
    for d in (0.5, 1, 2, 3, 4):                      # a new obsession this week
        c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (now - d * 86400, "music_played", "Hozier", "", 21, 3))
hot = [s for s, _ in P.lately()]
check("a new artist on repeat is 'lately'", hot[:1] == ["Hozier"], P.lately())
check("a steady old favourite is not", "Coldplay" not in hot, P.lately())
P.upsert("music", "Old Phase Band", 1, source="did", confidence=0.8)
with _sq.connect(P.DB) as c:
    c.execute("UPDATE facts SET last_seen=? WHERE item='Old Phase Band'", (now - 200 * 86400,))
P.invalidate()
check("a behaviour-only taste unseen for months fades from the summary", "Old Phase Band" not in P.summary())
check("'lately' shows in the summary", "Lately into: Hozier" in P.summary(), P.summary())

print("\n=== remembering what you did together ===")
with _sq.connect(P.DB) as c:
    c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (now - 26 * 3600, "asked",
              "make a PDF about how UPI payments work", "", 20, 4))
    c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (now - 60, "asked", "what's my battery", "", 20, 4))
P.record_episode("pause")
eps = P.recent_episodes()
check("an earlier session's request is remembered with a human time",
      any(e.startswith("yesterday") and "UPI" in e for e in eps), eps)
check("this session's requests and one-word commands are not", not any("battery" in e or "pause" in e for e in eps), eps)
P.invalidate()
check("they ride along in the summary", "Recently they asked you" in P.summary(), P.summary())
check("telling it about yourself is learned right away",
      bool(P._SELF_DISCLOSURE.search("honestly I love Hozier")) and not P._SELF_DISCLOSURE.search("play hozier"))

print("\n=== summary ===")
P.set_setting("name", "Sam")
P.invalidate()
s = P.summary()
check("the summary names them", "Name: Sam" in s, s)
check("the summary carries tastes", "Arctic Monkeys" in s and "The Mentalist" in s, s)
check("dislikes are marked", "dislikes Pineapple pizza" in s, s)
check("it stays short", len(s) < 1400, len(s))
check("it tells the model not to recite it", "never recite" in s)

print("\n=== control ===")
n = P.forget("Hozier")
check("forget removes matching facts", n >= 1 and "Hozier" not in items("music"))
P.set_setting("learning", "off")
P.observe_tool("play_music", {}, {"title": "x", "artist": "Nobody New", "app": "Spotify"})
check("with learning off nothing is learned", "Nobody New" not in items("music"))
P.set_setting("learning", "on")
check("forget everything empties it", P.forget(everything=True) >= 1 and not P.facts())

if os.environ.get("JARVIS_LIVE", "1") != "0":
    print("\n=== learning from what they say (real model) ===")
    from jarvis.core.llm import get_backend
    b = get_backend()
    b.load()
    L = P.Learner(b)
    for t in ["I'm about to sleep, good night",
              "play blinding lights",
              "honestly I love The Office, I've rewatched it like five times on Prime Video",
              "I can't stand horror movies",
              "my brother's name is Arjun, he's saved as Bhaiya",
              "what's the weather tomorrow"]:
        L._buf.append(t)
    n = L.flush()
    tv = items("movies_tv")
    check("a show they love is learned", any("office" in x.lower() for x in tv), P.facts())
    check("a dislike is learned as a dislike",
          any("horror" in f["item"].lower() and f["polarity"] == -1 for f in P.facts()), P.facts())
    check("a family member is learned", any("arjun" in (f["item"] + f["detail"]).lower() for f in P.facts("people")), P.facts("people"))
    check("passing remarks are not", not any("sleep" in f["item"].lower() for f in P.facts()), P.facts())
    check("a one-off play request is not a taste", not any("blinding" in f["item"].lower() for f in P.facts()), P.facts())

print(f"\n{sum(R)}/{len(R)} profile checks passed")
sys.exit(0 if all(R) else 1)
