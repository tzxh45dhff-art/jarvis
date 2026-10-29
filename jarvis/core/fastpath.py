"""
Instant answers for a handful of questions that never need a model.

"What time is it" took 4.9 s through the model (benchmark, Oct 2026) and
"what's my battery at" 9.2 s - for facts the Mac knows exactly. These are
answered here in milliseconds. The match is deliberately narrow: the whole
request must be one of these questions and nothing else, so anything longer or
different ("what time is it in Tokyo", "is my battery healthy") goes to the
model as before. Only read-only facts - never actions, whose outcome depends
on the screen.
"""
from __future__ import annotations

import re
import subprocess
import time

_LEAD = r"^\s*(hey\s+jarvis[,!]?\s*|jarvis[,!]?\s*|ok(ay)?[,!]?\s*|so[,!]?\s*)?(please\s+)?(can you\s+|could you\s+)?(tell me\s+)?"
_TAIL = r"\s*(please|now|right now|currently)?\s*[?.!]*\s*$"

_TIME = re.compile(_LEAD + r"(what('?s| is)\s+the\s+time|what\s+time\s+is\s+it|the\s+time|time)" + _TAIL, re.I)
_DATE = re.compile(_LEAD + r"(what('?s| is)\s+(the\s+date|today'?s\s+date|the\s+day|today)|"
                   r"what\s+day\s+is\s+(it|today)|what\s+date\s+is\s+(it|today)|"
                   r"today'?s\s+date)" + _TAIL, re.I)
_BATTERY = re.compile(_LEAD + r"(what('?s| is)\s+(my\s+|the\s+)?battery(\s+(at|level|percentage|percent))?|"
                      r"how\s+much\s+battery(\s+(do\s+i\s+have(\s+left)?|is\s+left|left))?|"
                      r"battery(\s+(level|percentage|percent|status))?)" + _TAIL, re.I)


def _battery() -> str | None:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True,
                             timeout=4).stdout
    except Exception:
        return None
    m = re.search(r"(\d+)%;\s*([^;]+);\s*([^\n]*)", out)
    if not m:
        return None
    pct, state, rest = int(m.group(1)), m.group(2).strip().lower(), m.group(3)
    plugged = "AC Power" in out
    left = re.search(r"(\d+:\d+) remaining", rest)
    if state.startswith("charged") or (plugged and pct >= 99):
        tail = " and fully charged"
    elif plugged and ("not charging" in state or "attached" in state or "finishing" in state):
        tail = ", plugged in" + (" and finishing charging" if "finishing" in state else "")
    elif plugged:
        tail = ", charging" + (f" ({left.group(1)} to full)" if left else "")
    else:
        tail = (f", about {left.group(1).replace(':', ' h ')} min left" if left else "")
    return f"Your battery is at {pct}%{tail}."


def answer(text: str) -> tuple[str, str] | None:
    """(answer, what it was) for an exact simple question, else None."""
    t = (text or "").strip()
    if not t or len(t) > 60:
        return None
    if _TIME.match(t):
        return f"It's {time.strftime('%I:%M %p').lstrip('0')}.", "time"
    if _DATE.match(t):
        return f"It's {time.strftime('%A, %d %B %Y').replace(' 0', ' ')}.", "date"
    if _BATTERY.match(t):
        b = _battery()
        return (b, "battery") if b else None
    return None


# ------------------------------------------------------------------ actions
#
# A handful of commands whose meaning never depends on judgement: "play",
# "pause", "next song", "volume up". Through the model each one cost a
# 3-5 s round trip before anything happened (and "play" once took three).
# Here they run the same tool the model would have called - with the same
# safety checks - and answer from its verified result. Anything that fails
# falls through to the model with a note of what was tried, so a fast path
# can never make a request worse than it was.

_PLAY_BARE = re.compile(_LEAD + r"(play|resume|continue|unpause|keep playing)"
                        r"(\s+(it|this|that|the\s+(music|song|track)|music|my\s+music|again))?"
                        + _TAIL, re.I)
_PAUSE = re.compile(_LEAD + r"(pause|stop|hold)(\s+(it|this|that|the\s+(music|song|track)|"
                    r"music|playback|playing|the\s+playback))?" + _TAIL, re.I)
_NEXT = re.compile(_LEAD + r"(next|skip)(\s+(it|this|that|one|song|track|this\s+(song|track)|"
                   r"the\s+(song|track)|to\s+the\s+next\s+(song|track)))?" + _TAIL, re.I)
_PREV = re.compile(_LEAD + r"(previous(\s+(song|track|one))?|(go\s+back|back|last)\s+(a\s+|one\s+)?(song|track)|"
                   r"play\s+the\s+(previous|last)\s+(song|track|one))" + _TAIL, re.I)
_PLAY_ON = re.compile(_LEAD + r"play\s+(?P<q>.+?)\s+(on|in|from|using)\s+"
                      r"(?P<app>spotify|apple\s+music|music|youtube(\s+music)?)" + _TAIL, re.I)
_PLAY_SONG = re.compile(_LEAD + r"play\s+(the\s+)?(?P<kind>song|track|album|artist|playlist)\s+"
                        r"(?P<q>.+?)" + _TAIL, re.I)
_PLAY_BY = re.compile(_LEAD + r"play\s+(?P<q>.+?\s+by\s+.+?)" + _TAIL, re.I)
_VOL_UP = re.compile(_LEAD + r"(turn\s+(it|the\s+volume|the\s+music)\s+up|volume\s+up|louder|"
                     r"(make\s+it\s+)?a\s+bit\s+louder|increase\s+(the\s+)?volume|raise\s+(the\s+)?volume)" + _TAIL, re.I)
_VOL_DOWN = re.compile(_LEAD + r"(turn\s+(it|the\s+volume|the\s+music)\s+down|volume\s+down|quieter|"
                       r"softer|(make\s+it\s+)?a\s+bit\s+quieter|lower\s+(the\s+)?volume|"
                       r"decrease\s+(the\s+)?volume|reduce\s+(the\s+)?volume)" + _TAIL, re.I)
_VOL_SET = re.compile(_LEAD + r"(set\s+(the\s+)?)?volume\s+(to\s+|at\s+)?(?P<n>\d{1,3})\s*(%|percent)?" + _TAIL, re.I)
_MUTE = re.compile(_LEAD + r"(mute|silence)(\s+(it|the\s+(sound|volume|mac|audio)))?" + _TAIL, re.I)
_UNMUTE = re.compile(_LEAD + r"unmute(\s+(it|the\s+(sound|volume|mac|audio)))?" + _TAIL, re.I)

_APPS = {"spotify": "Spotify", "apple music": "Music", "music": "Music",
         "youtube": "YouTube", "youtube music": "YouTube"}


# Apps where "play" / "pause" / "skip" mean the video in front, not music.
_VIDEO_FRONT = {"Safari", "Google Chrome", "Arc", "Firefox", "Brave Browser",
                "Microsoft Edge", "Opera", "TV", "QuickTime Player", "VLC", "IINA",
                "Netflix", "Prime Video", "Disney+"}


def _music_context() -> bool:
    """True when a bare play/pause/skip clearly means a music player: one is
    in front, or no video app is in front and a player has a song loaded."""
    try:
        from ..context import front_window, players
        front, _ = front_window()
        if front in ("Spotify", "Music"):
            return True
        if front in _VIDEO_FRONT:
            return False
        return any(p.get("state") in ("playing", "paused") for p in players())
    except Exception:
        return False


# "continue" / "resume" on their own are just as often an answer to "shall I
# go on?" as a request for music; with a song merely paused in the background
# they used to start the music instead of carrying on with the job.
_BARE_GO_ON = re.compile(_LEAD + r"(continue|resume)" + _TAIL, re.I)


def _music_in_front() -> bool:
    try:
        from ..context import front_window
        return front_window()[0] in ("Spotify", "Music")
    except Exception:
        return False


def action(text: str) -> tuple[str, dict] | None:
    """(tool, args) for an unambiguous media command, else None."""
    t = (text or "").strip()
    if not t or len(t) > 90:
        return None
    hit = _match(t)
    if hit and hit[0] in ("play_music", "control_playback") and not hit[1].get("query"):
        # bare transport commands follow what is on screen
        if not _music_context():
            return None
        if _BARE_GO_ON.match(t) and not _music_in_front():
            return None
    return hit


def _match(t: str) -> tuple[str, dict] | None:
    if _PLAY_BARE.match(t):
        return "play_music", {}
    if _PAUSE.match(t):
        return "control_playback", {"action": "pause"}
    if _NEXT.match(t):
        return "control_playback", {"action": "next"}
    if _PREV.match(t):
        return "control_playback", {"action": "previous"}
    if _MUTE.match(t):
        return "set_volume", {"mute": True}
    if _UNMUTE.match(t):
        return "set_volume", {"mute": False}
    if _VOL_UP.match(t):
        return "set_volume", {"change": 15}
    if _VOL_DOWN.match(t):
        return "set_volume", {"change": -15}
    m = _VOL_SET.match(t)
    if m and int(m.group("n")) <= 100:
        return "set_volume", {"level": int(m.group("n"))}
    m = _PLAY_ON.match(t)
    if m:
        app = _APPS[re.sub(r"\s+", " ", m.group("app").lower())]
        return "play_music", {"query": m.group("q").strip(" '\""), "app": app}
    m = _PLAY_SONG.match(t)
    if m:
        kind = {"track": "song"}.get(m.group("kind").lower(), m.group("kind").lower())
        return "play_music", {"query": m.group("q").strip(" '\""), "kind": kind}
    m = _PLAY_BY.match(t)
    if m and not re.search(r"\b(movie|film|show|episode|series|video|netflix|prime|hotstar)\b", t, re.I):
        return "play_music", {"query": m.group("q").strip(" '\""), "kind": "song"}
    return None


def reply(tool: str, args: dict, content) -> str:
    """A short spoken-style confirmation built from the verified result."""
    c = content if isinstance(content, dict) else {}
    np = c.get("now_playing") if isinstance(c.get("now_playing"), dict) else c
    title, artist = np.get("title"), np.get("artist")
    app = c.get("app") or ""
    on = f" on {app}" if app and app != "Music" else ""
    if tool == "play_music":
        if title:
            return f"Playing {title}" + (f" by {artist}" if artist else "") + on + "."
        return "Playing."
    if tool == "control_playback":
        a = args.get("action")
        if a == "pause":
            return "Paused."
        if a in ("next", "previous") and title:
            return f"{'Next' if a == 'next' else 'Back'}: {title}" + (f" by {artist}" if artist else "") + "."
        return "Done."
    if tool == "set_volume":
        if args.get("mute") is True:
            return "Muted."
        if args.get("mute") is False:
            return "Unmuted."
        lvl = c.get("level", c.get("volume"))
        return f"Volume {lvl}%." if lvl is not None else "Done."
    return "Done."
