"""
Where the user is actually working.

The assistant used to resolve every relative path against its own process
working directory, which is the directory it was launched from - its own source
tree. Asked to "fix the bug in llm.py" with an editor open on
~/Downloads/agentic ai, it searched jarvis/core/, found *its own* llm.py, and
edited that.

So the working directory is derived from what is on screen instead. Editors put
the open document in their window title, which the accessibility API exposes for
every app without needing to talk to each one:

    Electron  ->  "agentic ai - llm.py"
    Xcode     ->  "MyApp - ViewController.swift"

The title carries names, not paths, so Spotlight turns them back into a real
location. Spotlight is also what makes "find X" work anywhere on the disk in
milliseconds instead of walking the whole home directory.

Nothing here raises: every lookup degrades to the configured workspace, because
a wrong guess about the working directory must never stop a request.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("jarvis.context")

#: The assistant's own source tree. Nothing routine should ever write here.
INSTALL_ROOT = Path(__file__).resolve().parent.parent

# Window titles separate document from project with an em/en dash or a hyphen.
_SEP = re.compile(r"\s+[—–-]\s+")
# VS Code marks unsaved files with a bullet; Xcode appends " - Edited".
_NOISE = re.compile(r"^[●•*]\s*|\s+[—–-]\s+Edited$")

_CACHE: dict = {"at": 0.0, "value": None}
_LOCK = threading.Lock()
_TTL = 3.0          # a window title cannot change faster than a person can act


def _osascript(script: str, timeout: float = 4.0) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _windows_quartz() -> list[tuple[str, str]]:
    """
    (app, window title) for every window, front to back.

    Quartz is the right source rather than AppleScript: it reports the real
    application name ("Antigravity IDE", where the accessibility API says
    "Electron"), it needs no Apple Event round trip, and crucially it lists
    windows that are *not* on screen. That last point is the whole game - while
    the user is typing into the terminal their editor is behind it, so anything
    that only sees the front window sees the terminal and nothing else.
    """
    import Quartz
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []
    rows = []
    for w in wins:
        if w.get("kCGWindowLayer", 0) != 0:      # menu bar, docks, overlays
            continue
        owner = (w.get("kCGWindowOwnerName") or "").strip()
        title = (w.get("kCGWindowName") or "").strip()
        if owner and title:
            rows.append((owner, title))
    return rows


def _windows_applescript() -> list[tuple[str, str]]:
    """
    Fallback for when window names are unreadable.

    Quartz only fills in kCGWindowName when the process has Screen Recording
    permission; without it every title comes back empty and the accessibility
    API is the only way left to ask.
    """
    out = _osascript(r'''
      tell application "System Events"
        set acc to {}
        repeat with p in (every process whose background only is false)
          try
            set t to (value of attribute "AXTitle" of front window of p)
            if t is not missing value and t is not "" then
              set end of acc to ((name of p) & tab & t)
            end if
          end try
        end repeat
        set AppleScript's text item delimiters to linefeed
        set outStr to acc as text
        set AppleScript's text item delimiters to ""
        return outStr
      end tell''')
    rows = []
    for line in out.splitlines():
        if "\t" in line:
            app, _, title = line.partition("\t")
            if title.strip():
                rows.append((app.strip(), title.strip()))
    return rows


def _windows() -> list[tuple[str, str]]:
    try:
        rows = _windows_quartz()
    except Exception as e:
        log.debug("Quartz window list unavailable: %s", e)
        rows = []
    return rows or _windows_applescript()


def _parse_title(title: str) -> tuple[str, str] | None:
    """
    Split an editor window title into (filename, project folder).

    Editors disagree on the order - VS Code writes "file - project", Antigravity
    writes "project - file" - so the side that looks like a filename wins rather
    than trusting position.
    """
    title = _NOISE.sub("", title or "").strip()
    parts = [p.strip() for p in _SEP.split(title) if p.strip()]
    if len(parts) < 2:
        return None
    named = [p for p in parts if re.search(r"\.[A-Za-z0-9]{1,8}$", p)]
    if not named:
        return None
    fname = named[0]
    folder = next((p for p in parts if p != fname), "")
    return fname, folder


_LOCATE_CACHE: dict = {}
_LOCATE_TTL = 600.0


def locate(name: str, near: str = "", limit: int = 25, deep: bool = True) -> list[Path]:
    """
    Find a file anywhere on the disk by name, newest first.

    Spotlight answers in milliseconds over the whole volume, where walking the
    home directory took long enough to time out. `near` prefers hits whose path
    contains that folder name, which is what disambiguates the four llm.py files
    on this machine.
    """
    if not name:
        return []
    # Same name asked again within minutes (every turn asks about the window
    # on screen): answer from memory instead of re-running Spotlight.
    key = (name, near, deep)
    hit = _LOCATE_CACHE.get(key)
    if hit and time.time() - hit[0] < _LOCATE_TTL:
        return hit[1][:limit]
    try:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        r = subprocess.run(
            ["mdfind", f"kMDItemFSName == '{safe}'c"],
            capture_output=True, text=True, timeout=6 if deep else 1.5)
        hits = [Path(p) for p in r.stdout.splitlines() if p.strip()]
    except Exception:
        hits = []
    if not hits and not deep:
        # The quick path never walks the disk: that walk took 8 s and ran
        # before every single request whenever Spotlight missed a title.
        _LOCATE_CACHE[key] = (time.time(), [])
        return []
    if not hits:                       # Spotlight may not index every volume
        try:
            r = subprocess.run(
                ["find", str(Path.home()), "-maxdepth", "6", "-name", name,
                 "-not", "-path", "*/node_modules/*", "-not", "-path", "*/.*"],
                capture_output=True, text=True, timeout=8)
            hits = [Path(p) for p in r.stdout.splitlines() if p.strip()]
        except Exception:
            return []
    if near:
        low = near.lower()
        hits.sort(key=lambda p: (low not in str(p).lower(),
                                 -_mtime(p)))
    else:
        hits.sort(key=lambda p: -_mtime(p))
    _LOCATE_CACHE[key] = (time.time(), hits)
    if len(_LOCATE_CACHE) > 200:
        _LOCATE_CACHE.pop(next(iter(_LOCATE_CACHE)))
    return hits[:limit]


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _detect() -> dict:
    """The document and project currently open in an editor, if any."""
    for app, title in _windows():
        whole = _NOISE.sub("", title or "").strip()
        # A document whose own name contains a dash ("Plan — Draft.pdf") is
        # the whole title, not "folder - file"; try that reading first.
        if re.search(r"\.[A-Za-z0-9]{1,8}$", whole) and _SEP.search(whole):
            for hit in locate(whole, limit=4, deep=False):
                return {"app": app, "title": title, "file": hit, "folder": hit.parent}
        parsed = _parse_title(title)
        if not parsed:
            continue
        fname, folder = parsed
        for hit in locate(fname, near=folder, limit=8, deep=False):
            if not folder or folder.lower() in str(hit).lower():
                return {"app": app, "title": title, "file": hit,
                        "folder": hit.parent}
        # The name resolved to nothing on disk - still worth reporting the
        # title, since it tells the model what the user is looking at.
        return {"app": app, "title": title, "file": None, "folder": None}
    return {}


def active() -> dict:
    """Cached view of the frontmost editor. Never raises, never blocks long."""
    with _LOCK:
        if time.time() - _CACHE["at"] < _TTL and _CACHE["value"] is not None:
            return _CACHE["value"]
    try:
        v = _detect()
    except Exception as e:
        log.debug("editor detection failed: %s", e)
        v = {}
    with _LOCK:
        _CACHE.update(at=time.time(), value=v)
    return v


def invalidate() -> None:
    with _LOCK:
        _CACHE.update(at=0.0, value=None)


def workspace() -> Path:
    """
    The directory a bare filename should be resolved against.

    Preference order: an explicit override, then the folder of whatever is open
    in an editor, then the configured workspace. The assistant's own source tree
    is never the answer, even when it is the process working directory.
    """
    override = os.environ.get("JARVIS_WORKSPACE")
    if override:
        return Path(override).expanduser()
    folder = active().get("folder")
    if folder:
        return folder
    from .config import CONFIG
    return Path(CONFIG.workspace)


def resolve(path: str) -> Path:
    """
    Turn whatever the model said into a real absolute path.

    Handles the three things it actually produces: an absolute path, a path
    relative to the user's workspace, and a bare filename that exists somewhere
    else entirely (which Spotlight finds).
    """
    raw = str(path or "").strip()
    if not raw:
        return workspace()
    p = Path(os.path.expanduser(raw))
    if p.is_absolute():
        return p
    here = workspace() / p
    if here.exists():
        return here
    # A bare name that is not in the workspace: ask Spotlight before giving up.
    if os.sep not in raw:
        hits = locate(p.name, near=str(workspace()), limit=5)
        if hits:
            return hits[0]
    return here


def is_own_source(path) -> bool:
    """True when a path lands inside the assistant's own installation."""
    try:
        return INSTALL_ROOT == Path(path).resolve() or \
            INSTALL_ROOT in Path(path).resolve().parents
    except Exception:
        return False


def front_window() -> tuple[str, str]:
    """(app, window title) of the frontmost window, from Quartz: no Apple
    Event, no permission prompt, ~20 ms. (NSWorkspace's frontmost app goes
    stale in a process without a run loop, which the engine is.)"""
    try:
        import Quartz
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or []
        for w in wins:
            if w.get("kCGWindowLayer", 0) != 0:
                continue
            owner = (w.get("kCGWindowOwnerName") or "").strip().lstrip("\u200e")
            if owner in ("Window Server", "Dock", "JARVIS Companion", ""):
                continue
            b = w.get("kCGWindowBounds") or {}
            if (b.get("Width") or 0) < 120 or (b.get("Height") or 0) < 80:
                continue
            return owner, (w.get("kCGWindowName") or "").strip().lstrip("\u200e")
    except Exception as e:                                 # noqa: BLE001
        log.debug("front window unavailable: %s", e)
    return "", ""


_PLAYERS = ("Spotify", "Music")
# The app name must be a literal: AppleScript only loads an app's terms
# ("player state") when it can see which app at compile time.
_NP_SCRIPT = (
    'tell application "{app}"\n'
    '  if player state is stopped then return "stopped"\n'
    '  try\n'
    '    set t to current track\n'
    '    return (player state as text) & tab & (name of t) & tab & (artist of t)\n'
    '  on error\n'
    '    return (player state as text)\n'
    '  end try\n'
    'end tell')


def players() -> list[dict]:
    """State of every running music app, queried in parallel (~0.15 s).
    Never launches an app: only ones already running are asked."""
    try:
        from .tools import osa
        running = osa.running_apps()
    except Exception:
        return []
    apps = [a for a in _PLAYERS if a in running]
    out: dict[str, dict] = {}

    def ask(app: str) -> None:
        try:
            r = subprocess.run(["osascript", "-e", _NP_SCRIPT.replace("{app}", app)],
                               capture_output=True, text=True, timeout=2.5)
            line = r.stdout.strip()
            if r.returncode != 0 or not line:
                return
            if line == "stopped":
                out[app] = {"app": app, "state": "stopped"}
                return
            state, _, rest = line.partition("\t")
            title, _, artist = rest.partition("\t")
            if not title:
                out[app] = {"app": app, "state": "stopped"}
                return
            out[app] = {"app": app, "state": state, "title": title, "artist": artist}
        except Exception:
            pass

    threads = [threading.Thread(target=ask, args=(a,), daemon=True) for a in apps]
    for t in threads:
        t.start()
    for t in threads:
        t.join(2.6)
    return [out[a] for a in apps if a in out]


_SNAP: dict = {"at": 0.0, "value": None}


def snapshot(max_age: float = 1.5) -> dict:
    """What the user is looking at and listening to, right now. Fast enough
    to run before every request (target < 0.3 s), cached for a moment."""
    with _LOCK:
        if time.time() - _SNAP["at"] < max_age and _SNAP["value"] is not None:
            return _SNAP["value"]
    box: dict = {}
    t = threading.Thread(target=lambda: box.__setitem__("players", players()), daemon=True)
    t.start()
    app, title = front_window()
    ed = active()
    t.join(2.8)
    v = {"front_app": app, "front_title": title, "editor": ed,
         "players": box.get("players", [])}
    with _LOCK:
        _SNAP.update(at=time.time(), value=v)
    return v


def describe() -> str:
    """A few lines for the model: the front window, the open document, and
    what each music app is doing - so "play", "pause this", "what song is
    this" need no looking around first."""
    try:
        s = snapshot()
    except Exception:
        return ""
    lines = []
    app, title = s.get("front_app"), s.get("front_title")
    if app:
        lines.append(f"Front window: {app}" + (f" — {title}" if title and title != app else ""))
    a = s.get("editor") or {}
    if a.get("file"):
        lines.append(f"Open document: {a['file']} (in {a['app']}); "
                     f"unqualified file names refer to {a['folder']}")
    elif a.get("title") and a.get("app") != app:
        lines.append(f"Also open: {a['app']} — {a['title']}")
    for p in s.get("players") or []:
        if p.get("state") == "stopped":
            lines.append(f"{p['app']}: open, nothing loaded")
        else:
            lines.append(f"{p['app']}: {p.get('state')} — {p.get('title')} by {p.get('artist')}")
    if not lines:
        return ""
    return "[On screen now:\n- " + "\n- ".join(lines) + "]"
