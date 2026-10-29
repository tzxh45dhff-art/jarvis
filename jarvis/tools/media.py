"""
Music and audio control: Apple Music, Spotify, and system volume.

`play_music` searches the local library first (instant, offline) and falls back
to the Apple Music catalogue, so "play Chamber of Reflection" works whether or
not the track has been added to the library.
"""
from __future__ import annotations

import json
import re
import logging
import subprocess
import time
import urllib.parse
import urllib.request

from ..safety.policy import Risk
from . import osa
from .registry import REGISTRY, ToolResult

log = logging.getLogger("jarvis.media")

MUSIC = "Music"


def _now_playing(app: str = MUSIC) -> dict | None:
    r = osa.run(f'''on run argv
  tell application "{app}"
    if it is not running then return ""
    if player state is stopped then return ""
    set t to current track
    set out to (name of t) & "{osa.FS}" & (artist of t) & "{osa.FS}" & (album of t)
    set out to out & "{osa.FS}" & (player state as text)
    try
      set out to out & "{osa.FS}" & (player position as text)
      set out to out & "{osa.FS}" & (duration of t as text)
    end try
    return out
  end tell
end run''', timeout=15)
    if not r.ok or not r.out.strip():
        return None
    rows = osa.parse_records(r.out, ["title", "artist", "album", "state",
                                     "position", "duration"])
    return rows[0] if rows else None


@REGISTRY.tool(
    "now_playing",
    "What music is currently playing (title, artist, album, player state). "
    "Checks Apple Music and Spotify.",
    {"type": "object", "properties": {}, "required": []},
    risk=Risk.SAFE, category="read")
def now_playing():
    for app in (MUSIC, "Spotify"):
        if not osa.app_installed(app) or not osa.app_running(app):
            continue
        d = _now_playing(app)
        if d and d.get("title"):
            d["app"] = app
            return ToolResult(True, content=d,
                              display=f"{d['state']}: {d['title']} — {d['artist']}")
    return ToolResult(True, content={"playing": False},
                      display="nothing is playing")


def _catalog_lookup(query: str) -> dict | None:
    """
    Resolve a request to the right Apple Music catalogue song.

    The search returns covers, karaoke versions and live cuts alongside the
    original; the first row is often not the one meant. Candidates are ranked by
    how well title and artist match the request, with a small preference for
    the original (non-live, non-remix, non-karaoke) recording.
    """
    url = ("https://itunes.apple.com/search?"
           + urllib.parse.urlencode({"term": query, "entity": "song", "limit": 25}))
    try:
        with urllib.request.urlopen(url, timeout=12) as resp:
            data = json.load(resp)
    except Exception:
        return None
    want = _words(query)
    best, best_score = None, -1.0
    for i, r in enumerate(data.get("results", [])):
        if not r.get("trackViewUrl"):
            continue
        title, artist = _words(r.get("trackName")), _words(r.get("artistName"))
        cand = {"title": r.get("trackName"), "artist": r.get("artistName"),
                "album": r.get("collectionName")}
        score = 0.0
        if _matches(cand, query):
            score += 3.0
        score += len(want & title)
        # The ARTIST field is what says who recorded it. A cover that merely
        # has the artist's name in its title ("Yellow (Coldplay) [Fingerstyle]")
        # must not outrank the artist's own recording.
        if artist and artist <= want:
            score += 4.0
        elif artist & want:
            score += 1.0
        if want & artist and (want & artist) <= title and not artist <= want:
            score -= 2.0
        extra = (r.get("trackName") or "").lower()
        score -= 1.5 * sum(k in extra for k in (
            "live", "karaoke", "remix", "instrumental", "cover", "tribute",
            "acoustic", "fingerstyle", "demo", "piano", "lullaby", "tv version"))
        score -= 0.05 * i                       # the service's own ordering
        if score > best_score:
            best, best_score = r, score
    if best is None:
        return None
    return {"title": best.get("trackName"), "artist": best.get("artistName"),
            "album": best.get("collectionName"), "url": best["trackViewUrl"]}


def _active_player() -> str:
    """
    The music app a bare "play" / "pause" means, the way a person reads it:
    the player on screen, else the one playing, else the one with a song
    paused in it. Returns "" when no player has anything loaded.

    Used to be "Music if it is running", so with Spotify open on screen and a
    song paused there, "play" tried Apple Music, waited 8 s for it to fail and
    only then asked the model what to do.
    """
    try:
        from ..context import front_window, players
        front, _ = front_window()
        states = {p["app"]: p for p in players()}
    except Exception:
        return ""
    if front in states and states[front].get("state") != "stopped":
        return front
    for want in ("playing", "paused"):
        hits = [a for a, p in states.items() if p.get("state") == want]
        if len(hits) == 1:
            return hits[0]
        if hits:
            # Both have a song paused: the one whose window is nearer the
            # front was used more recently.
            try:
                from ..context import _windows_quartz
                order = [a for a, _ in _windows_quartz()]
                hits.sort(key=lambda a: order.index(a) if a in order else 99)
            except Exception:
                pass
            if front in hits:
                return front
            return hits[0]
    if front in states:
        return front
    return ""


def _preferred_music_app() -> str:
    """
    Which app "play X" means, the way a person would read it:

    1. the music app on screen - Spotify open in front means Spotify;
    2. the user's saved default (exact-keyed preference store, not fuzzy memory);
    3. the player already in use (playing, or a song paused in it);
    4. a running player, Spotify first; then Apple Music.

    The saved default used to come first, so a stale "Music" setting sent a
    song request to Apple Music while Spotify sat open on screen - which then
    could not play it and fell through to YouTube.
    """
    try:
        from ..context import front_window
        front = front_window()[0]
    except Exception:
        front = ""
    if front in ("Spotify", MUSIC):
        return front
    from ..memory.prefs import get_default
    pref = get_default("music", "")
    if pref and osa.app_installed(pref):
        return pref
    active = _active_player()            # the player in use
    if active:
        return active
    for a in ("Spotify", MUSIC):
        if osa.app_installed(a) and osa.app_running(a):
            return a
    return "Spotify" if osa.app_installed("Spotify") else MUSIC


def _words(text: str) -> set[str]:
    import re
    return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower())
            if len(w) > 2 and w not in ("the", "and", "feat", "remastered",
                                        "version", "remix", "live")}


def _matches(track: dict, want: str, kind: str = "song") -> bool:
    """
    Is the playing track what was asked for?

    Judged on the *title* for a song (artist words may also appear in the
    request). Matching the album once let "Viva la Vida" be satisfied by "Life
    in Technicolor" - the first track of the album called "Viva La Vida".
    """
    w = _words(want)
    if not w:
        return True
    title, artist = _words(track.get("title")), _words(track.get("artist"))
    if kind == "album":
        return bool(w & _words(track.get("album")))
    if kind == "artist":
        return bool(w & artist)
    if kind == "playlist":
        return True
    # song / auto: drop the artist's name from the request; at least half of
    # what remains must be in the track title.
    if w <= artist:                     # only an artist was asked for
        return True
    rest = w - artist
    hits = rest & title
    return bool(hits) and len(hits) >= (len(rest) + 1) // 2


def _verify_playing(app: str, want: str = "", timeout: float = 6.0,
                    kind: str = "song") -> dict | None:
    """
    Poll until the app reports it is genuinely playing.

    This exists because opening a page is not playing. The catalogue path used
    to `open` an album URL and report success; the album sat on screen, paused,
    while the assistant announced "now playing". A tool that cannot confirm its
    own effect must not claim it.
    """
    deadline = time.time() + timeout
    fallback = None
    while time.time() < deadline:
        d = _now_playing(app)
        if d and d.get("state") == "playing":
            if not want:
                return d
            if _matches(d, want, kind):
                return d
            # Something is playing, but not what was asked for — usually the
            # previous track still going. Reporting that as success is exactly
            # the lie this function exists to prevent.
            fallback = d
        time.sleep(0.35)
    if want and fallback:
        log.info("asked for %r but %r is playing; not claiming success",
                 want, fallback.get("title"))
        return None
    return fallback


_SPOTIFY_PLAY = (
    'on run argv\n'
    '  tell application "Spotify"\n'
    '    activate\n'
    '    play track (item 1 of argv)\n'
    '  end tell\n'
    'end run'
)
_SPOTIFY_RESUME = 'on run argv\ntell application "Spotify" to play\nend run'
_MUSIC_RESUME = 'on run argv\ntell application "Music" to play\nend run'

_PLAYLIST_SCRIPT = (
    'on run argv\n'
    '  set q to item 1 of argv\n'
    '  tell application "Music"\n'
    '    if (exists user playlist q) then\n'
    '      play user playlist q\n'
    '      return "playlist:" & q\n'
    '    end if\n'
    '  end tell\n'
    '  return ""\n'
    'end run'
)


def _library_script(field: str) -> str:
    return (
        'on run argv\n'
        '  set q to item 1 of argv\n'
        '  tell application "Music"\n'
        f'    set hits to (every track of library playlist 1 whose {field} contains q)\n'
        f'    if (count of hits) is 0 and "{field}" is "name" then\n'
        '      set hits to (every track of library playlist 1 whose artist contains q)\n'
        '    end if\n'
        '    if (count of hits) is 0 then return ""\n'
        '    set t to item 1 of hits\n'
        '    play t\n'
        f'    return (name of t) & "{osa.FS}" & (artist of t)\n'
        '  end tell\n'
        'end run'
    )


def _play_row(title: str, timeout: float = 14.0) -> dict | None:
    """
    On the album page Music is showing, double-click the row for `title` and
    return what is playing once it is that song; None if it could not be done.
    """
    try:
        from . import ax
        from . import control as C
        if not ax.available():
            return None
        deadline = time.time() + timeout
        want = title.strip().lower()
        row = None
        while time.time() < deadline and row is None:     # page needs a moment
            appel, _ = ax._app(MUSIC)
            win = ax._front_window(appel)
            queue, n = [win] if win is not None else [], 0
            while queue and n < 3000:
                e = queue.pop(0)
                n += 1
                role = ax._str(ax._attr(e, "AXRole"))
                if role == "AXCell" and ax._label(e, role).strip().lower() == want:
                    row = e
                    break
                queue.extend(ax._attr(e, "AXChildren") or [])
            if row is None:
                time.sleep(0.5)
        if row is None:
            return None
        ax._ax().AXUIElementPerformAction(row, "AXScrollToVisible")  # if off-screen
        time.sleep(0.3)
        pt = ax._center(row)
        if pt is None:
            return None
        C.focus_app(MUSIC)
        r = C.click(x=pt[0], y=pt[1], double=True)
        if not r.ok:
            return None
        d = _verify_playing(MUSIC, title, timeout=8)
        return d if d and _matches(d, title) else None
    except Exception as e:                     # never break playback on this
        log.info("row play failed: %s", e)
        return None


_SPOTIFY_LINK = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|artist|playlist)/([A-Za-z0-9]{22})")
_SPOTIFY_HIGHLIGHT = re.compile(r"spotify:track:([A-Za-z0-9]{22})")
_SPOTIFY_CACHE: dict[str, str] = {}


def _spotify_uri(query: str, kind: str = "auto") -> str:
    """
    A playable spotify: URI for what was asked, found through web search
    (~1 s, no Spotify account or API key). `spotify:search:` URIs only open
    a results page - they never play - which is why "play X on Spotify" used
    to end on YouTube.
    """
    key = f"{kind}|{query.lower().strip()}"
    if key in _SPOTIFY_CACHE:
        return _SPOTIFY_CACHE[key]
    from .web import _cascade
    want = {"song": ("track",), "auto": ("track", "album", "artist", "playlist"),
            "album": ("album",), "artist": ("artist",), "playlist": ("playlist",)}.get(kind, ("track",))
    words = _words(query) - {"the", "a", "by", "song", "on", "spotify"}
    best, best_score = "", -1.0
    for q in (f"site:open.spotify.com {query}", f"{query} spotify"):
        try:
            rows, _ = _cascade("text", q, 8, deadline=4.0)
        except Exception:
            rows = []
        for i, r in enumerate(rows):
            url = r.get("href") or ""
            m = _SPOTIFY_LINK.search(url)
            if not m:
                continue
            typ, sid = m.group(1), m.group(2)
            hl = _SPOTIFY_HIGHLIGHT.search(url)
            if typ == "album" and hl and "track" in want:
                typ, sid = "track", hl.group(1)
            if typ not in want:
                continue
            title = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", r.get("title") or "")
            hit = len(words & _words(title)) / max(1, len(words))
            # earlier results and the preferred kind win ties
            score = hit * 10 - i * 0.3 - want.index(typ) * 0.5
            if score > best_score:
                best, best_score = f"spotify:{typ}:{sid}", score
        if best and best_score >= 4:
            break
    if best:
        _SPOTIFY_CACHE[key] = best
    return best


def _spotify_play(query: str, kind: str = "auto") -> tuple[bool, dict | str]:
    """Find the exact track (or album/artist/playlist) and tell Spotify to play
    its URI; then confirm Spotify really is playing it."""
    if not osa.app_installed("Spotify"):
        return False, "Spotify is not installed"
    uri = _spotify_uri(query, kind)
    if not uri:
        return False, f"could not find {query!r} on Spotify"
    if not osa.app_running("Spotify"):
        subprocess.run(["open", "-g", "-a", "Spotify"], capture_output=True, timeout=15)
        time.sleep(2.5)
    r = osa.run(_SPOTIFY_PLAY, uri, timeout=20)
    if not r.ok:
        subprocess.run(["open", uri], capture_output=True, timeout=15)
        time.sleep(1.5)
        osa.run(_SPOTIFY_RESUME, timeout=10)
    # A whole album/artist/playlist plays from its own first track, so the
    # title check only applies to a single song.
    is_track = uri.startswith("spotify:track:")
    d = _verify_playing("Spotify", query if is_track else "", timeout=6,
                        kind="song")
    if not d and is_track:
        d = _verify_playing("Spotify", "", timeout=1.5)
        if d:
            d = {**d, "note": "playing, but the title differs from the request"}
    if d:
        return True, {**d, "uri": uri, "verified": True}
    return False, ("Spotify did not start playing. It may be logged out, or "
                   "showing a dialog.")



# ------------------------------------------------------------------ YouTube
#
# The last resort for music: a song that is in neither the library nor a
# subscription the user has. Opening a video is not the same as playing it -
# browsers may block autoplay - so sound is checked through macOS's own power
# assertions, which coreaudiod and browsers raise while audio is playing.

# Browsers raise these power assertions only while a page is actually playing
# media (seen live: Safari "WebKit Media Playback" and "HTMLMediaElement
# playback"). The generic coreaudiod output assertions are useless for this:
# a voice-capture aggregate device holds one permanently.
_BROWSER_MEDIA = ("webkit media playback", "htmlmediaelement playback",
                  "playing audio", "video wake lock", "audio wake lock")


def browser_media_ids() -> set[str] | None:
    """
    IDs of the browser media-playback assertions alive right now.

    IDs, not a yes/no: an assertion lingers a few seconds after its tab closes,
    and another tab may already be playing - so "playing" is judged by a NEW
    assertion appearing after the video was opened.
    """
    import re
    try:
        out = subprocess.run(["pmset", "-g", "assertions"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return None
    ids = set()
    for line in out.splitlines():
        if any(m in line.lower() for m in _BROWSER_MEDIA):
            m = re.search(r"\[(0x[0-9a-f]+)\]", line)
            ids.add(m.group(1) if m else line.strip())
    return ids


def browser_media_playing() -> bool | None:
    """True while a browser page is playing media; None if it can't tell."""
    ids = browser_media_ids()
    return None if ids is None else bool(ids)


audio_active = browser_media_playing          # name kept for callers/tests


def _youtube_top(query: str) -> dict | None:
    """The top video for a search, read from YouTube's results page."""
    import re
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode(
        {"search_query": query})
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Safari/605.1.15",
            "Accept-Language": "en"})
        with urllib.request.urlopen(req, timeout=12) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})".{0,1500}?'
                  r'"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"', html, re.S)
    if not m:
        m2 = re.search(r'"videoId":"([\w-]{11})"', html)
        return {"id": m2.group(1), "title": query} if m2 else None
    try:                                   # a JSON string literal, decoded as one
        title = json.loads('"' + m.group(2) + '"')
    except ValueError:
        title = m.group(2)
    return {"id": m.group(1), "title": title}


def _youtube_play(query: str) -> ToolResult:
    hit = _youtube_top(query)
    if not hit:
        return ToolResult(False, error=f"Could not find {query!r} on YouTube "
                                       f"(no network, or YouTube changed its page).")
    before = browser_media_ids()
    url = f"https://www.youtube.com/watch?v={hit['id']}"
    subprocess.run(["open", url], capture_output=True, timeout=15)
    playing = None
    deadline = time.time() + 12
    while time.time() < deadline and before is not None:
        now = browser_media_ids()
        if now is None:
            break
        if now - before:                    # a new playback started
            playing = True
            break
        time.sleep(0.5)
    content = {"source": "youtube", "title": hit["title"], "url": url,
               "verified": bool(playing)}
    if playing:
        return ToolResult(True, content=content,
                          display=f"playing {hit['title']} (YouTube)")
    content["note"] = ("Opened the video in the browser, but could not confirm "
                       "sound is playing - the browser may have blocked autoplay. "
                       "Tell the user it is open and may need a click on play.")
    return ToolResult(True, content=content,
                      display=f"opened {hit['title']} on YouTube (not confirmed playing)")

def _resume(app: str) -> ToolResult:
    script = _SPOTIFY_RESUME if app == "Spotify" else _MUSIC_RESUME
    r = osa.run(script, timeout=10)
    if not r.ok:
        return ToolResult(False, error=r.error or f"{app} did not respond")
    d = _verify_playing(app, timeout=3)
    if not d:
        return ToolResult(False, error=f"{app} did not resume playback.")
    return ToolResult(True, content={"resumed": True, "app": app, "verified": True, **d},
                      display=f"playing {d['title']} — {d['artist']} ({app})")


@REGISTRY.tool(
    "play_music",
    "Play music and confirm it is actually playing. Give a song, artist, album "
    "or playlist; call with no query to resume whichever player has a song "
    "on screen or paused. Without `app` it uses the user's default player, else "
    "the one in use. Spotify plays the exact track; Apple Music tries the "
    "library then the catalogue; YouTube is the last resort. Check `verified` "
    "before saying it is playing.",
    {"type": "object", "properties": {
        "query": {"type": "string",
                  "description": "Song, artist, album or playlist. Empty = resume."},
        "app": {"type": "string", "enum": ["Music", "Spotify", "YouTube"],
                "description": "Override which app to use. YouTube plays the top "
                               "video for the query in the browser"},
        "kind": {"type": "string",
                 "enum": ["auto", "song", "artist", "album", "playlist"]}},
     "required": []},
    risk=Risk.LOW, category="media", parallel_safe=False, slow=True)
def play_music(query: str = "", app: str = "", kind: str = "auto"):
    if not query.strip() and not app:
        # A bare "play": resume whatever player has the song, not a guess.
        active = _active_player()
        if active:
            return _resume(active)
    target = app or _preferred_music_app()
    if target == "YouTube":
        if not query.strip():
            return ToolResult(False, error="say what to play on YouTube")
        return _youtube_play(query)
    if not query.strip():
        return _resume(target)

    # The chosen app first, then the user's other music app, and only then
    # YouTube. Apple Music used to fall straight through to YouTube even with
    # Spotify installed and open.
    order = [target] + [a for a in ("Spotify", MUSIC)
                        if a != target and osa.app_installed(a)]
    misses: list[str] = []
    for a in order:
        r = _play_on_spotify(query, kind) if a == "Spotify" else _play_on_music(query, kind)
        if r.ok:
            if misses and isinstance(r.content, dict):
                r.content["note"] = (f"{misses[0].split(':')[0]} couldn't play it, so it's "
                                     f"playing on {a} instead")
            return r
        misses.append(f"{a}: {r.error}")
        log.info("play_music: %s could not play %r (%s)", a, query, r.error)
    if kind == "playlist":
        # A personal playlist is not on YouTube; say what happened instead.
        return ToolResult(False, error=(
            f"couldn't find a playlist called {query!r} ({'; '.join(misses)}). If it's "
            f"one of the user's own playlists, ask them to check its exact name."))
    yt = _youtube_play(query)
    if yt.ok and isinstance(yt.content, dict):
        yt.content["why_youtube"] = "; ".join(misses)
    return yt


def _play_on_spotify(query: str, kind: str) -> ToolResult:
    ok, res = _spotify_play(query, kind)
    if not ok:
        return ToolResult(False, error=str(res))
    return ToolResult(True, content={"app": "Spotify", **res},
                      display=f"playing {res['title']} — {res['artist']} (Spotify)")


def _play_on_music(query: str, kind: str) -> ToolResult:
    """Apple Music: a playlist, then the library, then the catalogue. Never
    falls back to anything else itself - play_music decides that."""
    if not osa.app_installed(MUSIC):
        return ToolResult(False, error="Apple Music is not installed")

    if kind in ("auto", "playlist"):
        r = osa.run(_PLAYLIST_SCRIPT, query, timeout=25)
        if r.ok and r.out.startswith("playlist:"):
            d = _verify_playing(MUSIC, timeout=6)
            if d:
                return ToolResult(True,
                                  content={"kind": "playlist", "playlist": query, **d},
                                  display=f"playing playlist {query}")

    field = {"artist": "artist", "album": "album"}.get(kind, "name")
    r = osa.run(_library_script(field), query, timeout=35)
    if r.ok and r.out.strip():
        d = _verify_playing(MUSIC, query, timeout=6, kind=kind if kind != "auto" else "song")
        if d:
            return ToolResult(True, content={"source": "library", **d, "verified": True},
                              display=f"playing {d['title']} — {d['artist']}")

    # ---------------- Apple Music catalogue ----------------
    hit = _catalog_lookup(query)
    if not hit:
        return ToolResult(False, error="not in the library or the Apple Music catalogue")
    subprocess.run(["open", "-a", "Music", hit["url"]], capture_output=True,
                   timeout=20)
    # The URL opens the album page; "play" there starts at track 1. So the
    # exact song's own row is double-clicked, then checked. Never skipped
    # through: stepping "next" runs off the end of the album into Apple
    # Music's autoplay queue (a Lana Del Rey track once played that way).
    d = _play_row(hit["title"])
    if d:
        return ToolResult(True, content={"source": "apple_music_catalog",
                                         **hit, **d, "verified": True},
                          display=f"playing {d['title']} — {d['artist']}")
    # No subscription, or not available here: play_music tries elsewhere.
    return ToolResult(False, error="Apple Music opened the track but would not play it "
                                   "(likely no subscription)")


@REGISTRY.tool(
    "control_playback",
    "Control music playback: pause, play, next track, previous track, stop, "
    "toggle shuffle or repeat.",
    {"type": "object", "properties": {
        "action": {"type": "string",
                   "enum": ["play", "pause", "playpause", "next", "previous",
                            "stop", "shuffle_on", "shuffle_off", "repeat_all",
                            "repeat_off"]},
        "app": {"type": "string", "enum": ["Music", "Spotify"]}},
     "required": ["action"]},
    risk=Risk.LOW, category="media", parallel_safe=False)
def control_playback(action: str, app: str = ""):
    target = app or _active_player() or next(
        (a for a in (MUSIC, "Spotify") if osa.app_installed(a) and osa.app_running(a)), MUSIC)
    if action == "stop" and target == "Spotify":
        action = "pause"                   # Spotify has no "stop" command
    verbs = {
        "play": "play", "pause": "pause", "playpause": "playpause",
        "next": "next track", "previous": "previous track", "stop": "stop",
    }
    if action in verbs:
        body = f'tell application "{target}" to {verbs[action]}'
    elif action == "shuffle_on":
        body = f'tell application "{target}" to set shuffle enabled to true'
    elif action == "shuffle_off":
        body = f'tell application "{target}" to set shuffle enabled to false'
    elif action == "repeat_all":
        body = f'tell application "{target}" to set song repeat to all'
    elif action == "repeat_off":
        body = f'tell application "{target}" to set song repeat to off'
    else:
        return ToolResult(False, error=f"unknown action {action!r}")
    before = _now_playing(target)
    r = osa.run(f"on run argv\n{body}\nend run", timeout=15)
    if not r.ok:
        return ToolResult(False, error=r.error)
    # Confirm the effect instead of assuming it: "paused" is only reported once
    # the player says paused, "next" only once the track actually changed.
    np, ok, why = None, True, ""
    deadline = time.time() + 3.0
    while time.time() < deadline:
        np = _now_playing(target) if action != "stop" else None
        state = (np or {}).get("state", "stopped")
        if action == "pause":
            ok = state in ("paused", "stopped")
        elif action == "play":
            ok = state == "playing"
        elif action == "playpause":
            ok = (state == "playing") != ((before or {}).get("state") == "playing")
        elif action in ("next", "previous"):
            ok = bool(np) and (not before or np.get("title") != before.get("title")
                               or action == "previous")
        elif action == "stop":
            ok = _now_playing(target) is None
        if ok:
            break
        time.sleep(0.3)
    if not ok:
        why = {"pause": "it is still playing", "play": "nothing started playing",
               "playpause": "the play state did not change",
               "next": "the track did not change", "previous": "nothing is playing",
               "stop": "it is still playing"}.get(action, "no change was seen")
        return ToolResult(False, content={"action": action, "app": target,
                                          "now_playing": np},
                          error=f"sent '{action}' to {target}, but {why}.")
    return ToolResult(True,
                      content={"action": action, "app": target,
                               "now_playing": np, "verified": True},
                      display=f"{action} ({target})"
                              + (f": {np['title']}" if np and np.get("title") else ""))


@REGISTRY.tool(
    "set_volume",
    "Set or adjust the Mac's output volume (0-100), or mute/unmute.",
    {"type": "object", "properties": {
        "level": {"type": "integer", "description": "Absolute level 0-100"},
        "change": {"type": "integer",
                   "description": "Relative change, e.g. -10 to turn down"},
        "mute": {"type": "boolean"}},
     "required": []},
    risk=Risk.LOW, category="media", parallel_safe=False)
def set_volume(level: int = None, change: int = 0, mute: bool = None):
    cur = osa.run('on run argv\nreturn output volume of (get volume settings)\nend run',
                  timeout=10)
    try:
        current = int(cur.out)
    except (ValueError, AttributeError):
        current = 50
    if mute is not None:
        osa.run(f'on run argv\nset volume output muted {str(bool(mute)).lower()}\nend run',
                timeout=10)
        return ToolResult(True, content={"muted": bool(mute), "volume": current},
                          display="muted" if mute else "unmuted")
    target = level if level is not None and level >= 0 else current + (change or 0)
    target = max(0, min(100, int(target)))
    r = osa.run('on run argv\nset volume output volume (item 1 of argv as integer)\n'
                'set volume output muted false\nend run', str(target), timeout=10)
    if not r.ok:
        return ToolResult(False, error=r.error)
    got = osa.run('on run argv\nreturn output volume of (get volume settings)\nend run',
                  timeout=10)
    try:
        now = int(got.out)
    except (ValueError, AttributeError):
        now = None
    # macOS snaps the volume to its own steps, so allow a couple of points.
    if now is not None and abs(now - target) > 3:
        return ToolResult(False, content={"asked": target, "now": now},
                          error=f"asked for volume {target} but it is {now} - the "
                                f"output device may not allow software volume "
                                f"(HDMI or some USB speakers).")
    return ToolResult(True, content={"volume": now if now is not None else target,
                                     "was": current, "verified": now is not None},
                      display=f"volume {current} → {now if now is not None else target}")


@REGISTRY.tool(
    "search_music",
    "Search your Apple Music library for songs, without playing anything.",
    {"type": "object", "properties": {
        "query": {"type": "string"}, "limit": {"type": "integer"}},
     "required": ["query"]},
    risk=Risk.SAFE, category="read")
def search_music(query: str, limit: int = 12):
    r = osa.run(f'''on run argv
  set q to item 1 of argv
  set lim to (item 2 of argv) as integer
  set out to ""
  tell application "Music"
    set hits to (every track of library playlist 1 whose name contains q or artist contains q or album contains q)
    set n to count of hits
    if n > lim then set n to lim
    repeat with i from 1 to n
      set t to item i of hits
      set out to out & (name of t) & "{osa.FS}" & (artist of t) & "{osa.FS}" & (album of t) & "{osa.RS}"
    end repeat
  end tell
  return out
end run''', query, str(limit), timeout=35)
    if not r.ok:
        return ToolResult(False, error=r.error)
    rows = osa.parse_records(r.out, ["title", "artist", "album"])
    return ToolResult(True, content={"query": query, "tracks": rows,
                                     "count": len(rows)},
                      display=f"{len(rows)} track(s) matching {query!r}")


@REGISTRY.tool(
    "video_state",
    "Is a video or song playing in the browser right now? Answers from macOS "
    "itself (a playing page holds a media-playback session), so it works even "
    "when the browser blocks scripting. Use it to confirm 'play X' on "
    "Netflix, YouTube, Prime, Hotstar, Spotify web... instead of reloading or "
    "re-opening the page to check.",
    {"type": "object", "properties": {}, "required": []},
    risk=Risk.SAFE, category="read", parallel_safe=True)
def video_state():
    ids = browser_media_ids()
    if ids is None:
        return ToolResult(True, content={"playing": None,
                                         "note": "could not query the system"},
                          display="could not tell")
    playing = bool(ids)
    return ToolResult(True, content={
        "playing": playing, "sessions": len(ids),
        "verified": playing,
        "note": ("something is playing in a browser" if playing else
                 "nothing is playing; the page may be paused, loading, or waiting "
                 "on a Play button or sign-in")},
        display="playing" if playing else "not playing")
