"""
Expressive speech: how replies are split and delivered, and the helper's
protocol. Offline - no speech model is loaded and nothing is played.
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.voice import mood
from jarvis.voice import speak_helper as H

R = []


def check(n, c, e=""):
    print(("  PASS " if c else "  FAIL ") + n + ("" if c else f"\n        {e}"))
    R.append(bool(c))


print("=== mood of a sentence ===")
cases = {
    "I'm sorry, I couldn't open that file.": "apologetic",
    "Unfortunately that failed.": "apologetic",
    "All done! The tests passed.": "excited",
    "Which one did you mean?": "curious",
    "Hello there, happy to help!": "cheerful",
    "Careful, this can't be undone.": "apologetic",   # "can't" wins: it is bad news
    "Be aware this is permanent.": "calm",
    "The file is in your Downloads folder.": "neutral",
}
for text, want in cases.items():
    got = mood.classify(text)
    check(f"{text!r} -> {want}", got == want, got)

print("\n=== the plan for a reply ===")
p = mood.plan("Done! Your file is saved. But I couldn't find the second one. "
              "It may have moved. Want me to look again?", "bubbly")
moods = [d.mood for _, d in p]
check("one entry per sentence", len(p) >= 4, moods)
check("good news first", moods[0] == "excited", moods)
check("bad news is soft", "apologetic" in moods, moods)
check("a neutral line after an apology stays gentle",
      moods[moods.index("apologetic") + 1] in ("apologetic", "curious"), moods)
check("the question is curious", moods[-1] == "curious", moods)
check("no pause after the last sentence", p[-1][1].pause == 0.0)
check("apologies are slower than excitement",
      mood.STYLES["bubbly"]["apologetic"][1] < mood.STYLES["bubbly"]["excited"][1])
for style in mood.STYLES:
    check(f"style {style!r} covers every mood",
          set(mood.STYLES[style]) >= {"neutral", "cheerful", "excited", "curious",
                                      "apologetic", "calm"})
check("unknown style falls back", len(mood.plan("Hi!", "nope")) == 1)
check("markdown never reaches the voice",
      "**" not in " ".join(s for s, _ in mood.plan("**Done** and `ls` ok.")))
check("code blocks are not read out",
      "rm -rf" not in " ".join(s for s, _ in mood.plan("Run:\n```\nrm -rf /\n```\nThen relax.")))
check("empty text gives an empty plan", mood.plan("   ") == [])

print("\n=== things that sound wrong read aloud ===")
joined = " ".join(s for s, _ in mood.plan("Saved it to /Users/me/Documents/Reports/q3-summary.pdf for you."))
check("a long path is spoken as its file name", "q3-summary.pdf" in joined and "Users" not in joined, joined)
check("a plain slash is left alone", "and/or" in mood.speakable("this and/or that"))
check("a home path is shortened too", mood.speakable("see ~/Downloads/a.txt now") == "see a.txt now")

print("\n=== audio helpers ===")
quiet = np.full(2400, 0.4, dtype=np.float32)
check("quiet audio is lifted", float(np.abs(H._level(quiet)).max()) > 0.7)
check("but never boosted past 2.5x (no amplified hiss)",
      float(np.abs(H._level(np.full(2400, 0.1, dtype=np.float32))).max()) <= 0.2501)
loud = np.full(2400, 0.95, dtype=np.float32)
check("loud audio is not boosted", float(np.abs(H._level(loud)).max()) <= 0.81)
check("silence is left alone", float(np.abs(H._level(np.zeros(100, np.float32))).max()) == 0.0)
faded = H._fade(np.ones(24000, dtype=np.float32))
check("chunk edges fade (no clicks)", faded[0] == 0.0 and faded[-1] == 0.0 and faded[12000] == 1.0)

print("\n=== helper protocol (stub voice) ===")


class FakeTTS:
    sample_rate = 24000

    def synthesize(self, text, voice=None, speed=None):
        FakeTTS.calls.append((text, voice, speed))
        return np.full(4800, 0.3, dtype=np.float32)


FakeTTS.calls = []


class FakeSpeaker:
    def __init__(self): self.queue, self.stopped, self.began = [], 0, 0
    speaking = False
    def stop(self): self.stopped += 1
    def begin(self): self.began += 1
    def enqueue(self, a): self.queue.append(a)
    def wait(self, timeout=0): pass


fake_sp = FakeSpeaker()
voice = H.ExpressiveVoice(tts=FakeTTS(), speaker=fake_sp)
voice.say("Hi! All done. Sorry, one thing failed.", "bubbly")
check("each sentence is synthesised with its own voice",
      len({v for _, v, _ in FakeTTS.calls}) >= 2, FakeTTS.calls)
check("audio is queued for playback", len(fake_sp.queue) == len(FakeTTS.calls))
check("playback is cut before new speech", fake_sp.stopped >= 1)

tmp = Path(tempfile.mkdtemp()) / "tts.sock"
H.SOCK = tmp
srv = H.Server(voice)
t = threading.Thread(target=srv.serve, daemon=True)
t.start()
for _ in range(50):
    if tmp.exists():
        break
    time.sleep(0.05)


def ask(obj):
    c = socket.socket(socket.AF_UNIX)
    c.settimeout(3)
    c.connect(str(tmp))
    c.sendall((json.dumps(obj) + "\n").encode())
    out = json.loads(c.makefile().readline())
    c.close()
    return out


check("ping answers", ask({"cmd": "ping"}).get("ok") is True)
n0 = len(FakeTTS.calls)
check("say is accepted", ask({"cmd": "say", "text": "Hello there!", "style": "warm"}).get("ok"))
time.sleep(0.3)
check("and spoken", len(FakeTTS.calls) > n0)
check("stop is accepted", ask({"cmd": "stop"}).get("ok"))
check("unknown commands are refused politely", ask({"cmd": "dance"}).get("ok") is False)
check("quit ends the server", ask({"cmd": "quit"}).get("ok"))
time.sleep(1.3)
check("socket file is cleaned up", not tmp.exists())

print(f"\n{sum(R)}/{len(R)} expressive-voice checks passed")
sys.exit(0 if all(R) else 1)
