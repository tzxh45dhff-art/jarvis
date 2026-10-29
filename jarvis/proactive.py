"""
Suggestions JARVIS offers without being asked - quickly, or not at all.

The companion reports a moment as it happens (an app came to the front, the
app opened, the Mac woke, a periodic tick) and gets an answer in well under a
second: rules over the learned profile, the calendar and the machine's state,
never a model call. A suggestion that would arrive after the moment has passed
("continue The Mentalist?" five minutes into a film) is worse than none, so
anything slow is skipped and the companion drops answers older than a few
seconds.

Restraint is enforced here, not hoped for:
  * a daily budget, quiet hours, and a cool-down per suggestion;
  * nothing while something is already playing;
  * "don't suggest this again" is permanent; ignored three times = a week off;
  * an autonomy setting: off / suggest (default) / auto (only harmless
    actions - starting music, opening an app - are ever done automatically).
"""
from __future__ import annotations

import datetime as dt
import logging
import time
import uuid

from .memory import profile

log = logging.getLogger("jarvis.proactive")

DAILY_BUDGET = 6
COOLDOWN_S = 6 * 3600
QUIET = (23, 8)                      # no suggestions 23:00-08:00
_MUSIC_APPS = {"Spotify": "Spotify", "Music": "Music"}
_PLATFORM_HOSTS = {"Netflix": "netflix", "Prime Video": "primevideo",
                   "JioHotstar": "hotstar", "YouTube": "youtube", "Disney+": "disneyplus"}
_PENDING: dict[str, dict] = {}       # id -> suggestion, for feedback


# ------------------------------------------------------------------ settings

def mode() -> str:
    # Off unless the user turns it on (menu > Suggestions): on hold while the
    # triggers are made reliable.
    return profile._setting("suggestions", "off")            # off | suggest | auto


def _key_state(key: str) -> dict:
    import json
    raw = profile._setting(f"sugg:{key}", "")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _save_key_state(key: str, st: dict) -> None:
    import json
    profile.set_setting(f"sugg:{key}", json.dumps(st))


def _today_count() -> int:
    day = time.strftime("%Y-%m-%d")
    raw = profile._setting("sugg_day", "")
    d, _, n = raw.partition("|")
    return int(n) if d == day and n.isdigit() else 0


def _bump_today() -> None:
    day = time.strftime("%Y-%m-%d")
    profile.set_setting("sugg_day", f"{day}|{_today_count() + 1}")


def _allowed(key: str, now: float | None = None, ignore_quiet: bool = False) -> bool:
    now = now or time.time()
    if mode() == "off":
        return False
    hour = time.localtime(now).tm_hour
    if not ignore_quiet and (hour >= QUIET[0] or hour < QUIET[1]):
        return False
    if _today_count() >= DAILY_BUDGET:
        return False
    st = _key_state(key)
    if st.get("never"):
        return False
    if now - st.get("last", 0) < COOLDOWN_S:
        return False
    if st.get("snooze_until", 0) > now:
        return False
    return True


def feedback(sid: str, outcome: str) -> None:
    """outcome: accepted | dismissed | ignored | never"""
    s = _PENDING.pop(sid, None)
    if not s:
        return
    st = _key_state(s["key"])
    if outcome == "never":
        st["never"] = True
    elif outcome == "accepted":
        st["accepted"] = st.get("accepted", 0) + 1
        st["ignored"] = 0
    elif outcome in ("ignored", "dismissed"):
        st["ignored"] = st.get("ignored", 0) + 1
        if st["ignored"] >= 3:
            st["snooze_until"] = time.time() + 7 * 86400
            st["ignored"] = 0
    _save_key_state(s["key"], st)
    log.info("suggestion %s (%s): %s", s["key"], s["text"][:50], outcome)


def _make(key: str, text: str, prompt: str = "", auto_ok: bool = False,
          ignore_quiet: bool = False) -> dict | None:
    if not _allowed(key, ignore_quiet=ignore_quiet):
        return None
    sid = uuid.uuid4().hex[:10]
    s = {"type": "suggestion", "id": sid, "key": key, "text": text,
         "prompt": prompt, "auto": bool(auto_ok and prompt and mode() == "auto"),
         "at": time.time()}
    st = _key_state(key)
    st["last"] = time.time()
    _save_key_state(key, st)
    _bump_today()
    _PENDING[sid] = s
    return s


# ------------------------------------------------------------------ what we know

def _top(category: str, n: int = 3) -> list[dict]:
    return [f for f in profile.facts(category, min_conf=0.55) if f["polarity"] > 0][:n]


def _usual_music() -> tuple[str, str]:
    """(artist or '', app) the user most likely wants now."""
    r = profile.routine()
    artist = r["music"][0][0] if r["music"] else ""
    if not artist:
        hot = profile.lately()                 # their current phase beats old favourites
        artist = hot[0][0] if hot else ""
    if not artist:
        tops = [f["item"] for f in _top("music", 5) if "music" not in f["item"].lower()]
        artist = tops[0] if tops else ""
    app = "Spotify" if any("spotify" in f["item"].lower() for f in _top("apps", 5)) else ""
    return artist, app


def _calendar_ready() -> bool:
    """Only read the calendar when access is already granted: a suggestion
    must never cause a permission prompt (that call can block for 45 s)."""
    try:
        import EventKit as EK
        return EK.EKEventStore.authorizationStatusForEntityType_(0) in (3, 4)
    except Exception:
        return False


def _something_playing() -> bool:
    try:
        from .context import players
        if any(p.get("state") == "playing" for p in players()):
            return True
        from .tools.media import browser_media_playing
        return bool(browser_media_playing())
    except Exception:
        return False


# ------------------------------------------------------------------ moments

def suggest(event: dict) -> dict | None:
    """The one suggestion for this moment, or None. Fast: no model, no web."""
    if not profile.learning_enabled() or mode() == "off":
        return None
    kind = event.get("kind", "")
    try:
        if kind == "app_activated":
            return _on_app(event.get("app", ""), event.get("title", ""))
        if kind == "tick":
            return _on_tick()
        if kind in ("opened", "woke"):
            return _on_open()
    except Exception as e:                                    # noqa: BLE001
        log.debug("suggest failed: %s", e)
    return None


def _on_app(app: str, title: str) -> dict | None:
    app = (app or "").lstrip("‎")
    if app in _MUSIC_APPS:
        if _something_playing():
            return None
        artist, _ = _usual_music()
        if not artist:
            return None
        return _make(f"music:{app}", f"Play some {artist}?",
                     prompt=f"play {artist} on {'Spotify' if app == 'Spotify' else 'Apple Music'}",
                     auto_ok=True)
    # A streaming site in front, nothing playing yet: offer what they were watching there.
    low = f"{app} {title}".lower().replace(" ", "")
    for platform, host in _PLATFORM_HOSTS.items():
        if host in low or platform.lower().replace(" ", "") in low:
            if _something_playing():
                return None
            shows = [f for f in _top("movies_tv", 8)
                     if platform.lower() in (f["detail"] or "").lower()] or _top("movies_tv", 1)
            if not shows:
                return None
            show = shows[0]["item"]
            return _make(f"watch:{platform}:{show}", f"Continue {show}?",
                         prompt=f"play {show} on {platform}")
    return None


def _on_open() -> dict | None:
    """First open of the day (or waking the Mac in the morning): a short
    briefing from things already on this Mac - calendar and usual music."""
    day = time.strftime("%Y-%m-%d")
    if profile._setting("briefed", "") == day:
        return None
    hour = time.localtime().tm_hour
    if hour < 5 or hour >= 13:
        return None
    bits = []
    name = profile._setting("name", "")
    greet = ("Morning" if hour < 12 else "Hi") + (f", {name}" if name else "") + "!"
    try:
        if not _calendar_ready():
            raise PermissionError
        from .tools.pim import _events
        now = dt.datetime.now()
        end = now.replace(hour=23, minute=59)
        evs = [e for e in _events(now, end) if not e.get("all_day")]
        if evs:
            first = evs[0]
            when = str(first.get("start", ""))[11:16]
            bits.append(f"{len(evs)} event{'s' if len(evs) != 1 else ''} today, first "
                        f"{first.get('title', 'one')} at {when}.")
    except Exception:
        pass
    for d in profile.upcoming(1):
        bits.append(profile.describe_date(d)[0].upper() + profile.describe_date(d)[1:] + ".")
    artist, app = _usual_music()
    prompt = ""
    if artist and not _something_playing():
        bits.append(f"Want some {artist}?")
        prompt = f"play {artist}" + (f" on {app}" if app else "")
    if not bits:
        return None
    s = _make("briefing", greet + " " + " ".join(bits), prompt=prompt, ignore_quiet=True)
    if s:
        profile.set_setting("briefed", day)
    return s


def _date_nudge() -> dict | None:
    """The day before (and the morning of) someone's birthday or anniversary:
    offer a reminder or help with a wish - once per date per year."""
    hour = time.localtime().tm_hour
    if hour < 9 or hour >= 22:
        return None
    for d in profile.upcoming(1):
        year = d["date"][:4]
        key = f"date:{d['person']}:{d['label']}:{year}:{d['in_days']}"
        said = profile.describe_date(d)
        said = said[0].upper() + said[1:]
        if d["person"] == "me":
            if d["in_days"] == 0 and d["label"] == "birthday":
                return _make(key, "Happy birthday! 🎉 Want a playlist for the day?",
                             prompt="play a happy birthday party playlist")
            continue
        if d["in_days"] == 1:
            return _make(key, f"{said}. Want a reminder tomorrow morning to wish them?",
                         prompt=f"remind me tomorrow at 9am to wish {d['person']} a happy {d['label']}")
        if d["in_days"] == 0:
            return _make(key, f"{said}! Want help writing a message?",
                         prompt=f"help me write a warm {d['label']} message for {d['person']}")
    return None


def _on_tick() -> dict | None:
    """Every minute or so: things with a deadline (a meeting soon, battery)."""
    s = _date_nudge()
    if s:
        return s
    # meeting in the next 10 minutes
    try:
        if not _calendar_ready():
            raise PermissionError
        from .tools.pim import _events
        now = dt.datetime.now()
        for e in _events(now, now + dt.timedelta(minutes=11)):
            if e.get("all_day") or not e.get("start") or e["start"] <= now.isoformat(timespec="minutes"):
                continue
            title = e.get("title") or "Your event"
            s = _make(f"meeting:{title}:{str(e.get('start'))[:16]}",
                      f"{title} starts in about 10 minutes.", ignore_quiet=False)
            if s:
                return s
    except Exception:
        pass
    # battery low while unplugged
    try:
        import re
        import subprocess
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True,
                             timeout=3).stdout
        m = re.search(r"(\d+)%;\s*discharging", out)
        if m and int(m.group(1)) <= 15:
            return _make("battery", f"Battery's at {m.group(1)}%. Plug in soon?",
                         ignore_quiet=True)
    except Exception:
        pass
    return None
