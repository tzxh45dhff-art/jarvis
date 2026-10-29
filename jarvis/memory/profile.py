"""
What JARVIS knows about the person it works for, learned as it goes.

Three ways in:

* **What they say.** Every few turns (or when the conversation goes quiet) the
  user's own words are read by the model once, in the background, for durable
  facts - "likes Arctic Monkeys", "watches The Mentalist on Netflix". Passing
  remarks ("I'm about to sleep") are not facts and are dropped. This replaced
  saving raw sentences verbatim as "preferences".
* **What they do.** What plays in Spotify or Music, which app is in front at
  which hour, what JARVIS itself was asked to play or open. Stored as counts on
  this Mac only: never screenshots, keystrokes or message contents.
* **What they tell it directly** ("remember...", "forget...").

Everything lives in one SQLite file under ~/.jarvis/data. A short summary rides
along with each request (in the user turn, never the cached system prompt), so
"play something" means *their* kind of something.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from ..config import DATA_DIR

log = logging.getLogger("jarvis.profile")

DB = Path(os.environ.get("JARVIS_PROFILE_DB") or DATA_DIR / "profile.sqlite3")

CATEGORIES = {
    "identity": "About them",
    "music": "Music",
    "movies_tv": "Movies & shows",
    "streaming": "Streaming",
    "apps": "Apps",
    "food": "Food",
    "people": "People",
    "routine": "Routines",
    "style": "How they like answers",
    "work": "Work & study",
    "places": "Places",
    "other": "Other",
}
# Never learned unless the user explicitly asks to remember it.
_SENSITIVE = re.compile(
    r"\b(password|passcode|pin\b|otp|cvv|card number|bank account|ifsc|aadhaar|pan card|"
    r"ssn|salary|debt|loan|diagnos|disease|illness|medicat|therapy|depress|anxiety|"
    r"pregnan|sexual|religio|caste|politic)\w*", re.I)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    item TEXT NOT NULL,
    polarity INTEGER NOT NULL DEFAULT 1,       -- 1 likes / 0 neutral fact / -1 dislikes
    detail TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.6,
    source TEXT NOT NULL DEFAULT 'said',       -- said | did | told
    hits INTEGER NOT NULL DEFAULT 1,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    UNIQUE(category, item)
);
CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    hour INTEGER NOT NULL,
    weekday INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, subject);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dates (
    id INTEGER PRIMARY KEY,
    person TEXT NOT NULL,                       -- 'me' for the user
    label TEXT NOT NULL,                        -- birthday, anniversary, ...
    month INTEGER NOT NULL,
    day INTEGER NOT NULL,
    year INTEGER,
    source TEXT NOT NULL DEFAULT 'said',        -- said | contacts | told
    UNIQUE(person, label)
);
"""

_LOCK = threading.RLock()


def _conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=10, check_same_thread=False)
    c.executescript(_SCHEMA)
    return c


# ------------------------------------------------------------------ settings

def _setting(key: str, default: str) -> str:
    with _LOCK, _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def learning_enabled() -> bool:
    if os.environ.get("JARVIS_LEARNING") == "0":
        return False
    return _setting("learning", "on") == "on"


def watching_enabled() -> bool:
    """Learning from what the user does (players, front app), on its own switch."""
    return learning_enabled() and _setting("watching", "on") == "on"


# ------------------------------------------------------------------ personality
#
# How JARVIS talks, set by the user (menu, or "be more formal"). Only what
# differs from the defaults is sent with a request.

PERSONALITY = {
    "length": {"short": "Keep answers very short: one or two sentences unless asked for more.",
               "balanced": "",
               "detailed": "Give fuller answers with useful detail and context."},
    "tone": {"casual": "Talk casually, like a friend: relaxed, warm, contractions, no stiffness.",
             "friendly": "",
             "formal": "Be polite and professional; no slang or jokes."},
    "language": {"auto": "",
                 "english": "Always reply in English, even if the user writes in Hindi or Hinglish.",
                 "hinglish": "Reply in natural Hinglish (Hindi and English mixed, written in Roman "
                             "letters, never Devanagari), the way the user talks."},
    "humour": {"on": "A little light humour is welcome when the moment fits.", "off": ""},
}
PERSONALITY_DEFAULTS = {"length": "balanced", "tone": "friendly", "language": "auto", "humour": "off"}


def personality() -> dict:
    return {k: _setting(f"p_{k}", v) for k, v in PERSONALITY_DEFAULTS.items()}


def set_personality(**kw) -> dict:
    for k, v in kw.items():
        if k in PERSONALITY and v in PERSONALITY[k]:
            set_setting(f"p_{k}", v)
    invalidate()
    return personality()


def personality_note() -> str:
    lines = [PERSONALITY[k][v] for k, v in personality().items()
             if PERSONALITY.get(k, {}).get(v)]
    return "[How the user wants you to talk: " + " ".join(lines) + "]" if lines else ""


# ------------------------------------------------------------------ facts

def _norm(item: str) -> str:
    return re.sub(r"\s+", " ", (item or "").strip().strip(".")).strip()


def upsert(category: str, item: str, polarity: int = 1, detail: str = "",
           confidence: float = 0.6, source: str = "said") -> bool:
    """Add or reinforce a fact. Repeats raise confidence; a change of heart
    (likes -> dislikes) replaces the old polarity rather than averaging it."""
    category = category if category in CATEGORIES else "other"
    item = _norm(item)
    if not item or len(item) > 120 or _SENSITIVE.search(item + " " + (detail or "")):
        return False
    now = time.time()
    with _LOCK, _conn() as c:
        row = c.execute("SELECT id, polarity, confidence, hits FROM facts "
                        "WHERE category=? AND lower(item)=lower(?)", (category, item)).fetchone()
        if row:
            fid, pol, conf, hits = row
            if pol != polarity and source in ("said", "told"):
                c.execute("UPDATE facts SET polarity=?, confidence=?, detail=?, source=?, "
                          "hits=1, last_seen=? WHERE id=?",
                          (polarity, confidence, detail or "", source, now, fid))
            else:
                new_conf = min(0.99, max(conf, confidence) + 0.08)
                c.execute("UPDATE facts SET confidence=?, hits=hits+1, last_seen=?, "
                          "detail=CASE WHEN ?<>'' THEN ? ELSE detail END WHERE id=?",
                          (new_conf, now, detail or "", detail or "", fid))
        else:
            c.execute("INSERT INTO facts(category, item, polarity, detail, confidence, "
                      "source, hits, first_seen, last_seen) VALUES(?,?,?,?,?,?,1,?,?)",
                      (category, item, polarity, detail or "", confidence, source, now, now))
    return True


def facts(category: str | None = None, min_conf: float = 0.0) -> list[dict]:
    q = ("SELECT id, category, item, polarity, detail, confidence, source, hits, "
         "first_seen, last_seen FROM facts WHERE confidence >= ?")
    args: list = [min_conf]
    if category:
        q += " AND category = ?"
        args.append(category)
    q += " ORDER BY confidence * (1 + min(hits, 10) / 10.0) DESC, last_seen DESC"
    with _LOCK, _conn() as c:
        rows = c.execute(q, args).fetchall()
    keys = ["id", "category", "item", "polarity", "detail", "confidence", "source",
            "hits", "first_seen", "last_seen"]
    return [dict(zip(keys, r)) for r in rows]


def forget(query: str = "", everything: bool = False) -> int:
    """Delete facts whose item, detail or category matches; or everything."""
    with _LOCK, _conn() as c:
        if everything:
            n = c.execute("SELECT count(*) FROM facts").fetchone()[0]
            c.execute("DELETE FROM facts")
            c.execute("DELETE FROM events")
            return n
        like = f"%{query.strip()}%"
        cur = c.execute("DELETE FROM facts WHERE item LIKE ? OR detail LIKE ? OR category LIKE ?",
                        (like, like, like))
        c.execute("DELETE FROM events WHERE subject LIKE ? OR detail LIKE ?", (like, like))
        return cur.rowcount


def forget_id(fid: int) -> bool:
    with _LOCK, _conn() as c:
        return c.execute("DELETE FROM facts WHERE id=?", (int(fid),)).rowcount > 0


# ------------------------------------------------------------------ routines
#
# "When I say study mode, open Notion and Safari, play lo-fi, turn on Do Not
# Disturb." Saved as plain requests, run in order when the phrase is said.

def save_routine(name: str, steps: list[str]) -> bool:
    name = _norm(name).lower()
    steps = [str(x).strip() for x in steps if str(x).strip()][:12]
    if not name or not steps:
        return False
    set_setting(f"routine:{name}", json.dumps(steps))
    return True


def routines() -> dict[str, list[str]]:
    with _LOCK, _conn() as c:
        rows = c.execute("SELECT key, value FROM settings WHERE key LIKE 'routine:%'").fetchall()
    out = {}
    for k, v in rows:
        try:
            out[k[len("routine:"):]] = json.loads(v)
        except ValueError:
            pass
    return out


def delete_routine(name: str) -> bool:
    with _LOCK, _conn() as c:
        return c.execute("DELETE FROM settings WHERE key=?",
                         (f"routine:{_norm(name).lower()}",)).rowcount > 0


def match_routine(text: str) -> tuple[str, list[str]] | None:
    """The saved routine a request names outright ("study mode", "start study
    mode", "jarvis, study mode please"), else None."""
    t = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    t = re.sub(r"\b(hey|jarvis|please|start|run|begin|activate|turn on|do|my|the|routine|now)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t or len(t) > 40:
        return None
    for name, steps in routines().items():
        if t == name:
            return name, steps
    return None


# ------------------------------------------------------------------ important dates

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                       "sep", "oct", "nov", "dec"], 1)}


def parse_day(text: str) -> tuple[int, int, int | None] | None:
    """'12 March', 'March 12', '12/03', '2001-03-12', '12 Mar 1975' -> (month, day, year)."""
    t = (text or "").strip().lower()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})$", t)
    if m:
        return int(m.group(2)), int(m.group(3)), int(m.group(1))
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3,})\.?(?:,?\s+(\d{4}))?", t) or None
    if m and m.group(2)[:3] in _MONTHS:
        return _MONTHS[m.group(2)[:3]], int(m.group(1)), int(m.group(3)) if m.group(3) else None
    m = re.search(r"([a-z]{3,})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", t)
    if m and m.group(1)[:3] in _MONTHS:
        return _MONTHS[m.group(1)[:3]], int(m.group(2)), int(m.group(3)) if m.group(3) else None
    m = re.match(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?$", t)   # day/month (India)
    if m:
        y = m.group(3)
        y = int(y) + (2000 if y and len(y) == 2 and int(y) < 50 else 1900 if y and len(y) == 2 else 0) if y else None
        return int(m.group(2)), int(m.group(1)), y
    return None


def save_date(person: str, label: str, month: int, day: int, year: int | None = None,
              source: str = "said") -> bool:
    person = _norm(person) or "me"
    if person.lower() in ("i", "my", "mine", "myself", "user", "the user"):
        person = "me"
    label = (_norm(label) or "birthday").lower()
    try:
        import datetime as _dt
        _dt.date(2024, int(month), int(day))          # leap-year safe check
    except (ValueError, TypeError):
        return False
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO dates(person, label, month, day, year, source) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(person, label) DO UPDATE SET month=excluded.month, day=excluded.day, "
                  "year=COALESCE(excluded.year, dates.year), "
                  "source=CASE WHEN dates.source='told' AND excluded.source='contacts' "
                  "THEN dates.source ELSE excluded.source END",
                  (person, label, int(month), int(day), int(year) if year else None, source))
    invalidate()
    return True


def dates(person: str = "") -> list[dict]:
    q = "SELECT id, person, label, month, day, year, source FROM dates"
    args: list = []
    if person:
        q += " WHERE lower(person) LIKE ?"
        args.append(f"%{person.lower()}%")
    with _LOCK, _conn() as c:
        rows = c.execute(q, args).fetchall()
    keys = ["id", "person", "label", "month", "day", "year", "source"]
    return [dict(zip(keys, r)) for r in rows]


def upcoming(days: int = 14, today=None) -> list[dict]:
    """Dates in the next `days` days, soonest first, with how far away and the
    age or anniversary number when the year is known."""
    import datetime as _dt
    today = today or _dt.date.today()
    out = []
    for d in dates():
        for y in (today.year, today.year + 1):
            try:
                when = _dt.date(y, d["month"], d["day"])
            except ValueError:                        # 29 Feb in a non-leap year
                when = _dt.date(y, 3, 1)
            delta = (when - today).days
            if 0 <= delta <= days:
                item = dict(d, date=when.isoformat(), in_days=delta,
                            turning=(when.year - d["year"]) if d["year"] else None)
                out.append(item)
                break
    out.sort(key=lambda x: x["in_days"])
    return out


def describe_date(d: dict) -> str:
    who = "your" if d["person"] == "me" else f"{d['person']}'s"
    when = {0: "today", 1: "tomorrow"}.get(d["in_days"], f"in {d['in_days']} days")
    extra = ""
    if d.get("turning") and d["label"] == "birthday":
        extra = f" (turning {d['turning']})"
    elif d.get("turning") and "anniversary" in d["label"]:
        extra = f" ({d['turning']} years)"
    return f"{who} {d['label']} is {when}{extra}"


def import_contact_dates() -> int:
    """Birthdays and dated events saved in Contacts. Only when access is
    already granted: this must never trigger a permission prompt."""
    try:
        import Contacts as CN
        if CN.CNContactStore.authorizationStatusForEntityType_(0) != 3:
            return 0
        st = CN.CNContactStore.alloc().init()
        keys = [CN.CNContactGivenNameKey, CN.CNContactFamilyNameKey, CN.CNContactNicknameKey,
                CN.CNContactBirthdayKey, CN.CNContactDatesKey]
        req = CN.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
        found: list = []

        def visit(c, stop):
            name = (c.nickname() or " ".join(x for x in (c.givenName(), c.familyName()) if x)).strip()
            if not name:
                return True
            b = c.birthday()
            if b is not None and b.month() and b.day() and b.month() < 13:
                y = b.year() if 0 < b.year() < 3000 else None
                found.append((name, "birthday", b.month(), b.day(), y))
            for lv in (c.dates() or []):
                v = lv.value()
                label = str(CN.CNLabeledValue.localizedStringForLabel_(lv.label() or "") or "date").lower()
                if v is not None and v.month() and v.day() and v.month() < 13:
                    y = v.year() if 0 < v.year() < 3000 else None
                    found.append((name, label, v.month(), v.day(), y))
            return True

        st.enumerateContactsWithFetchRequest_error_usingBlock_(req, None, visit)
        n = sum(save_date(*f, source="contacts") for f in found)
        if n:
            log.info("imported %d date(s) from Contacts", n)
        return n
    except Exception as e:                                    # noqa: BLE001
        log.debug("contact dates unavailable: %s", e)
        return 0


# ------------------------------------------------------------------ behaviour

def observe(kind: str, subject: str, detail: str = "", source: str = "did") -> None:
    """Record something the user did. Counts become facts once they repeat."""
    if not learning_enabled() or not (subject or "").strip():
        return
    subject = _norm(subject)[:120]
    now = time.localtime()
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO events(ts, kind, subject, detail, hour, weekday) "
                  "VALUES(?,?,?,?,?,?)",
                  (time.time(), kind, subject, (detail or "")[:200], now.tm_hour, now.tm_wday))
        n = c.execute("SELECT count(*) FROM events WHERE kind=? AND lower(subject)=lower(?)",
                      (kind, subject)).fetchone()[0]
    # Asking JARVIS for it once is a clear signal; something merely seen
    # playing or in front has to repeat before it counts as a taste.
    if kind == "music_played" and (source == "asked" or n >= 3):
        upsert("music", subject, 1, detail=f"played {n}x", source="did",
               confidence=min(0.9, 0.45 + 0.08 * n))
    elif kind == "watched" and (source == "asked" or n >= 2):
        upsert("movies_tv", subject, 1, detail=detail, source="did",
               confidence=min(0.9, 0.5 + 0.1 * n))
    elif kind == "streaming" and (source == "asked" or n >= 3):
        upsert("streaming", subject, 1, detail=f"used {n}x", source="did",
               confidence=min(0.9, 0.45 + 0.08 * n))
    elif kind == "music_app" and n >= 3:
        upsert("apps", f"{subject} for music", 1, source="did",
               confidence=min(0.9, 0.45 + 0.05 * n))


# ------------------------------------------------------------------ episodes
#
# What the user and JARVIS did together, so it can say "last night you had me
# make that UPI PDF" the way a person remembers - and pick up where things
# were left. Requests only (no replies, no screen content), on this Mac.

_TRIVIAL = re.compile(r"^\W*(hi|hey|hello|thanks|thank you|ok(ay)?|yes|no|stop|pause|play|"
                      r"resume|next|skip|what time is it|what'?s the time)\W*$", re.I)


def record_episode(request: str) -> None:
    t = re.sub(r"\s+", " ", (request or "").strip())
    if len(t.split()) < 3 or _TRIVIAL.match(t) or not learning_enabled():
        return
    now = time.localtime()
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO events(ts, kind, subject, detail, hour, weekday) VALUES(?,?,?,?,?,?)",
                  (time.time(), "asked", t[:160], "", now.tm_hour, now.tm_wday))


def recent_episodes(n: int = 4, older_than_s: int = 15 * 60, within_s: int = 3 * 86400) -> list[str]:
    """The user's last few requests from earlier sessions (not this one),
    newest first, with a human time: 'yesterday 23:01: play The Night We Met'."""
    now = time.time()
    with _LOCK, _conn() as c:
        rows = c.execute("SELECT ts, subject FROM events WHERE kind='asked' AND ts < ? AND ts > ? "
                         "ORDER BY ts DESC LIMIT ?",
                         (now - older_than_s, now - within_s, n * 3)).fetchall()
    out, seen = [], set()
    today = time.strftime("%Y-%m-%d")
    yday = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
    for ts, subject in rows:
        key = subject.lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        when = ("today " if day == today else "yesterday " if day == yday
                else time.strftime("%a ", time.localtime(ts))) + time.strftime("%H:%M", time.localtime(ts))
        out.append(f"{when}: {subject}")
        if len(out) >= n:
            break
    return out


_STREAMING = {"netflix": "Netflix", "primevideo": "Prime Video", "hotstar": "JioHotstar",
              "jiocinema": "JioCinema", "youtube": "YouTube", "disneyplus": "Disney+",
              "sonyliv": "SonyLIV", "zee5": "ZEE5", "spotify": "Spotify", "twitch": "Twitch"}


def observe_tool(name: str, args: dict, content) -> None:
    """Learn from what the user asked JARVIS to do (a confirmed result only)."""
    if not learning_enabled():
        return
    c = content if isinstance(content, dict) else {}
    try:
        if name == "play_music":
            artist = c.get("artist") or ""
            if artist:
                observe("music_played", artist.split(",")[0].split("&")[0].strip(),
                        c.get("title", ""), source="asked")
            app = c.get("app") or args.get("app")
            if app:
                observe("music_app", app, source="asked")
        elif name in ("browser_open", "open_url"):
            url = (args.get("url") or "").lower()
            for key, platform in _STREAMING.items():
                if key in url:
                    observe("streaming", platform, url[:120], source="asked")
                    q = re.search(r"[?&]q=([^&]+)", url)
                    if q:
                        from urllib.parse import unquote_plus
                        observe("watched", unquote_plus(q.group(1)).title(), platform, source="asked")
                    break
    except Exception as e:                                    # noqa: BLE001
        log.debug("observe_tool failed: %s", e)


def routine(hour: int | None = None, weekday: int | None = None,
            days: int = 21) -> dict:
    """What usually happens around this hour: apps in front, music playing.
    Weekdays and weekends are kept apart - Saturday morning is not Monday's."""
    now = time.localtime()
    hour = now.tm_hour if hour is None else hour
    weekday = now.tm_wday if weekday is None else weekday
    weekend = weekday >= 5
    since = time.time() - days * 86400
    hours = ((hour - 1) % 24, hour, (hour + 1) % 24)
    out: dict = {"apps": [], "music": []}
    with _LOCK, _conn() as c:
        for kind, key in (("app_used", "apps"), ("music_played", "music")):
            rows = c.execute(
                f"SELECT subject, count(DISTINCT date(ts, 'unixepoch', 'localtime')) AS days "
                f"FROM events WHERE kind=? AND ts>=? AND hour IN (?,?,?) "
                f"AND (weekday>=5)=? GROUP BY subject HAVING days>=2 "
                f"ORDER BY days DESC LIMIT 4",
                (kind, since, *hours, int(weekend))).fetchall()
            out[key] = [(s, d) for s, d in rows]
    return out


def lately(kind: str = "music_played", days: int = 10, baseline: int = 50,
           min_plays: int = 3) -> list[tuple[str, int]]:
    """What the user is into right now: things played at least `min_plays`
    times in the last `days`, at well over their usual rate. A taste that
    shifts (a new artist on repeat) shows up here within days, long before it
    outweighs years of old favourites."""
    now = time.time()
    with _LOCK, _conn() as c:
        rows = c.execute(
            "SELECT subject, sum(ts >= ?) AS recent, sum(ts < ? AND ts >= ?) AS older "
            "FROM events WHERE kind=? AND ts >= ? GROUP BY lower(subject)",
            (now - days * 86400, now - days * 86400, now - (days + baseline) * 86400,
             kind, now - (days + baseline) * 86400)).fetchall()
    out = []
    for subject, recent, older in rows:
        recent, older = int(recent or 0), int(older or 0)
        if recent >= min_plays and recent / days >= 2 * (older / baseline):
            out.append((subject, recent))
    out.sort(key=lambda x: -x[1])
    return out[:4]


# A taste only seen in behaviour, not said, fades if it has not shown up for
# this long: someone's 2024 phase should not decide what plays today.
STALE_DAYS = 90


# ------------------------------------------------------------------ summary

_SUMMARY_CACHE: dict = {"at": 0.0, "text": None}


def summary(max_chars: int = 1100) -> str:
    """A compact "about the user" block for the model, or "" when nothing is known."""
    if time.time() - _SUMMARY_CACHE["at"] < 60 and _SUMMARY_CACHE["text"] is not None:
        return _SUMMARY_CACHE["text"]
    lines: list[str] = []
    try:
        name = _setting("name", "")
        if name:
            lines.append(f"Name: {name}")
        by_cat: dict[str, list[dict]] = {}
        stale = time.time() - STALE_DAYS * 86400
        for f in facts(min_conf=0.5):
            if f["source"] == "did" and f["last_seen"] < stale:
                continue
            by_cat.setdefault(f["category"], []).append(f)
        for cat, label in CATEGORIES.items():
            rows = by_cat.get(cat) or []
            if not rows:
                continue
            likes = [r["item"] + (f" ({r['detail']})" if r["detail"] and cat != "music" else "")
                     for r in rows if r["polarity"] > 0][:8]
            facts_ = [r["item"] + (f": {r['detail']}" if r["detail"] else "")
                      for r in rows if r["polarity"] == 0][:6]
            dislikes = [r["item"] for r in rows if r["polarity"] < 0][:5]
            bits = []
            if likes:
                bits.append(("likes " if cat in ("music", "movies_tv", "food") else "") + ", ".join(likes))
            if facts_:
                bits.append("; ".join(facts_))
            if dislikes:
                bits.append("dislikes " + ", ".join(dislikes))
            lines.append(f"{label}: " + "; ".join(bits))
        past = recent_episodes()
        if past:
            lines.append("Recently they asked you (earlier sessions): " + " | ".join(past))
        soon = upcoming(7)
        if soon:
            lines.append("Coming up: " + "; ".join(describe_date(d) for d in soon[:4]))
        hot = lately()
        if hot:
            lines.append("Lately into: " + ", ".join(f"{s} ({n} plays in the last 10 days)"
                                                       for s, n in hot))
        r = routine()
        usual = [s for s, _ in r["apps"]]
        tunes = [s for s, _ in r["music"]]
        if usual or tunes:
            when = time.strftime("%A %H:00")
            lines.append(f"Usually around this time ({when}): "
                         + ", ".join(usual + [f"listening to {t}" for t in tunes]))
    except Exception as e:                                    # noqa: BLE001
        log.debug("profile summary failed: %s", e)
    text = ""
    if lines:
        body = "\n".join("- " + l for l in lines)
        text = ("[What you know about the user (learned over time; use it naturally "
                "when it helps - e.g. pick their kind of music or their usual app - "
                "and never recite it unprompted):\n" + body[:max_chars] + "]")
    _SUMMARY_CACHE.update(at=time.time(), text=text)
    return text


def invalidate() -> None:
    _SUMMARY_CACHE.update(at=0.0, text=None)


# ------------------------------------------------------------------ learning from words

EXTRACT_PROMPT = """You maintain a private profile of a person from what they say to their assistant.
Read their messages below and list DURABLE facts about them: tastes (music, artists,
movies, shows, streaming platforms, food), preferences about how they want things
done, people in their life (family names and nicknames), their work or study,
routines, places, and things they dislike.

Rules:
- Only lasting facts. "I'm about to sleep", "play X", "what's the weather" are NOT facts
  (a request to play an artist once is not a taste - skip it).
- Keep the specifics a friend would remember: names ("girlfriend: Simran", not just
  "girlfriend"), the reason or context ("Blender - for animation projects"), the artist
  of a favourite song. A vague fact with no detail is not worth storing.
- Corrections are preferences: "no, use Spotify not Apple Music", "don't ask me every
  time", "shorter answers please" -> record how they want things done (category
  apps or style).
- A change of mind replaces the old fact: "I'm not into Coldplay anymore" -> polarity -1.
- Already known (do not repeat unless it changed or adds detail): {known}
- Never include passwords, money, health, religion, politics or anything sensitive.
- Use their own wording for names (artists, shows, people). Resolve obvious typos
  ("the weekend" the singer = "The Weeknd").
- Return ONLY a JSON array, no prose. Each item:
  {"category": one of [identity, music, movies_tv, streaming, apps, food, people,
   routine, style, work, places, other],
   "item": short name of the thing, "polarity": 1 (likes) | 0 (neutral fact) | -1 (dislikes),
   "detail": optional short context, "confidence": 0.5-0.95}
- Important dates (birthdays, anniversaries, exams, deadlines) of the person or the
  people they mention: {"category": "date", "person": "Mom" | "me" | name,
  "label": "birthday" | "anniversary" | short label, "date": "12 March" (add the
  year if they said it)}.
- If there is nothing durable, return [].

Their messages:
"""


# Something the user says about themselves is learned right away, the way a
# person would remember it, instead of waiting for a batch.
_SELF_DISCLOSURE = re.compile(
    r"\b(i (really |honestly |absolutely |kinda |kind of )?(love|like|hate|adore|prefer|enjoy|"
    r"can'?t stand|don'?t like|dislike|am|'m|work|study|live|usually|always|never)|"
    r"my (name|birthday|favou?rite|mom|mum|dad|brother|sister|girlfriend|boyfriend|wife|"
    r"husband|friend|best friend|boss|college|school|job|exam)|call me|"
    r"remember (that|this)|from now on|don'?t ever|i'?m (a|an|from|into|allergic))\b", re.I)


class Learner:
    """Turns what the user says into lasting facts, in the background: right
    away for things they say about themselves, otherwise a few turns at a
    time (one small model call per batch, not per message).

    The pending batch is shared by every Learner in the process: Stop replaces
    the Agent (and with it this object), and what the user said just before
    must not be lost with it."""

    BATCH = 4
    IDLE_FLUSH = 90.0
    _shared: dict = {"buf": [], "timer": None, "running": False}
    _lock = threading.Lock()

    def __init__(self, llm):
        self.llm = llm

    @property
    def _buf(self) -> list[str]:
        return self._shared["buf"]

    def note(self, user_text: str, reply: str = "") -> None:
        if not learning_enabled():
            return
        t = (user_text or "").strip()
        if len(t) < 6:
            return
        record_episode(t)
        entry = t[:500]
        if reply:
            # What JARVIS had just said, so "yes, that one is my favourite"
            # can be understood.
            entry += f"  (JARVIS had replied: {reply.strip()[:220]})"
        now = bool(_SELF_DISCLOSURE.search(t))
        with self._lock:
            self._shared["buf"].append(entry)
            full = len(self._shared["buf"]) >= self.BATCH
            if self._shared["timer"]:
                self._shared["timer"].cancel()
            tm = threading.Timer(self.IDLE_FLUSH, self.flush)
            tm.daemon = True
            tm.start()
            self._shared["timer"] = tm
        if full or now:
            threading.Thread(target=self.flush, daemon=True, name="profile-learn").start()

    def flush(self) -> int:
        with self._lock:
            if self._shared["running"] or not self._shared["buf"]:
                return 0
            batch, self._shared["buf"] = self._shared["buf"], []
            self._shared["running"] = True
        try:
            return self._extract(batch)
        except Exception as e:                                # noqa: BLE001
            log.info("profile extraction failed: %s", e)
            with self._lock:                                  # try again next time
                self._shared["buf"][:0] = batch[-8:]
            return 0
        finally:
            self._shared["running"] = False

    def _extract(self, batch: list[str]) -> int:
        known = "; ".join(f"{f['category']}: {f['item']}" for f in facts(min_conf=0.5)[:40])
        prompt = (EXTRACT_PROMPT.replace("{known}", known or "nothing yet")
                  + "\n".join(f"- {m}" for m in batch))
        out = []
        llm = self.llm
        try:
            from ..core.azure import AzureBackend, background
            if isinstance(llm, AzureBackend):
                llm = background()
        except Exception:
            pass
        for d in llm.stream([{"role": "user", "content": prompt}], tools=None,
                            think=False, max_tokens=500):
            if d.kind == "text":
                out.append(d.text)
        raw = "".join(out).strip()
        m = re.search(r"\[.*\]", raw, re.S)
        if not m:
            return 0
        try:
            items = json.loads(m.group(0))
        except ValueError:
            return 0
        n = 0
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            if it.get("category") == "date":
                when = parse_day(str(it.get("date", "")))
                if when and save_date(str(it.get("person", "me")), str(it.get("label", "birthday")),
                                      when[0], when[1], when[2], source="said"):
                    n += 1
                continue
            try:
                conf = float(it.get("confidence", 0.6))
            except (TypeError, ValueError):
                conf = 0.6
            if conf < 0.5:
                continue
            pol = it.get("polarity", 1)
            pol = pol if pol in (-1, 0, 1) else 1
            if upsert(str(it.get("category", "other")), str(it.get("item", "")), pol,
                      str(it.get("detail", ""))[:120], min(conf, 0.95), source="said"):
                n += 1
        if n:
            invalidate()
            log.info("learned %d fact(s) about the user", n)
        return n


# ------------------------------------------------------------------ watching

class Watcher:
    """Notices what the user does, every `every` seconds: the app in front and
    what is playing. Counts only, kept on this Mac. Off with the 'watching'
    setting (or learning) - and never reads windows' contents."""

    def __init__(self, every: float = 45.0):
        self.every = every
        self._stop = threading.Event()
        self._last_track: tuple = ()
        self._last_app: tuple = ("", 0.0)

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="profile-watch").start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            try:
                if watching_enabled():
                    self.tick()
            except Exception as e:                            # noqa: BLE001
                log.debug("watch tick failed: %s", e)

    def tick(self) -> None:
        from .. import context
        app, title = context.front_window()
        now = time.time()
        if app and (app != self._last_app[0] or now - self._last_app[1] > 600):
            observe("app_used", app)
            self._last_app = (app, now)
        low = f"{app} {title}".lower()
        for key, platform in _STREAMING.items():
            if key in low.replace(" ", "") and platform not in ("Spotify",):
                observe("streaming", platform)
                break
        for p in context.players():
            if p.get("state") == "playing" and p.get("title"):
                track = (p["app"], p["title"])
                if track != self._last_track:
                    self._last_track = track
                    observe("music_played", (p.get("artist") or "").split(",")[0].strip(),
                            p["title"])
                    observe("music_app", p["app"])
