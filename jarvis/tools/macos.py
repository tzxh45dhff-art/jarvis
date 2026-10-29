"""
macOS system integration: state, notifications, clipboard, Shortcuts, browser
context, focus modes.

These are the tools that let JARVIS answer with awareness of what is actually
happening on the machine rather than in the abstract.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from ..safety.policy import Risk, Verdict
from . import osa
from .registry import REGISTRY, ToolResult


def _sh(cmd: list[str], timeout: int = 12) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


@REGISTRY.tool(
    "system_status",
    "Current state of the Mac: battery, power, disk space, uptime, network, "
    "focus mode, volume, frontmost app and what is playing. Use for "
    "'how's my battery', 'am I on wifi', 'what am I running'.",
    {"type": "object", "properties": {}, "required": []},
    risk=Risk.SAFE, category="read")
def system_status():
    # Six independent probes. Run sequentially they cost ~1.4 s, which is most
    # of the answer's latency; in parallel they cost about as much as the
    # slowest one.
    import concurrent.futures as cf

    def battery():
        b = _sh(["pmset", "-g", "batt"], 8)
        m = re.search(r"(\d+)%;\s*([^;]+);\s*([^\n]*)", b)
        out = {"on_ac": "AC Power" in b}
        if m:
            out["battery"] = {"percent": int(m.group(1)),
                              "state": m.group(2).strip(),
                              "remaining": m.group(3).strip()
                                           .replace(" present: true", "")}
        return out

    def disk():
        lines = _sh(["df", "-H", "/"], 8).splitlines()
        if len(lines) > 1:
            p = lines[1].split()
            if len(p) >= 5:
                return {"disk": {"size": p[1], "used": p[2], "free": p[3],
                                 "used_pct": p[4]}}
        return {}

    def uptime():
        up = _sh(["uptime"], 6)
        out = {}
        m = re.search(r"up\s+(.*?),\s+\d+\s+user", up)
        if m:
            out["uptime"] = m.group(1)
        m = re.search(r"load averages?:\s*([\d.]+)", up)
        if m:
            out["load"] = float(m.group(1))
        return out

    def wifi():
        w = _sh(["networksetup", "-getairportnetwork", "en0"], 8)
        if "Current Wi-Fi Network:" in w:
            return {"wifi": w.split(":", 1)[1].strip()}
        return {"wifi": "not connected"} if w else {}

    def volume_and_front():
        out = {}
        r = osa.run('on run argv\n'
                    'set v to output volume of (get volume settings)\n'
                    'tell application "System Events" to set f to name of '
                    'first application process whose frontmost is true\n'
                    'return (v as text) & "' + osa.FS + '" & f\n'
                    'end run', timeout=10)
        if r.ok and osa.FS in r.out:
            v, f = r.out.split(osa.FS, 1)
            try:
                out["volume"] = int(v)
            except ValueError:
                pass
            out["frontmost_app"] = f.strip()
        return out

    def focus():
        try:
            p = Path.home() / "Library/DoNotDisturb/DB/Assertions.json"
            if p.exists():
                data = json.loads(p.read_text() or "{}")
                return {"focus_on": bool(data.get("data", [{}])[0]
                                         .get("storeAssertionRecords"))}
        except Exception:
            pass
        return {}

    def playing():
        try:
            from .media import _now_playing
            np = _now_playing()
            if np and np.get("title"):
                return {"now_playing":
                        f"{np['title']} — {np['artist']} ({np['state']})"}
        except Exception:
            pass
        return {}

    out: dict = {}
    probes = [battery, disk, uptime, wifi, volume_and_front, focus, playing]
    with cf.ThreadPoolExecutor(max_workers=len(probes)) as ex:
        for fut in [ex.submit(p) for p in probes]:
            try:
                out.update(fut.result(timeout=15) or {})
            except Exception:
                continue

    bits = []
    if "battery" in out:
        bits.append(f"battery {out['battery']['percent']}%")
    if "disk" in out:
        bits.append(f"{out['disk']['free']} free")
    if out.get("wifi"):
        bits.append(f"wifi {out['wifi']}")
    return ToolResult(True, content=out, display=", ".join(bits) or "system ok")


_BROWSERS = ("Safari", "Google Chrome", "Brave Browser", "Arc", "Microsoft Edge")


running_apps = osa.running_apps


@REGISTRY.tool(
    "browser_tabs",
    "List the pages currently open in the user's browsers, with titles and URLs. "
    "Use when the user says 'this page', 'what I'm looking at', or 'check this'.",
    {"type": "object", "properties": {
        "limit": {"type": "integer", "description": "Max tabs to return, default 40"}},
     "required": []},
    risk=Risk.SAFE, category="read")
def browser_tabs(limit: int = 40):
    # Querying each browser costs an osascript round trip, and Safari with many
    # windows is slow. Ask which browsers are running once, then query the live
    # ones concurrently and cap the work each one does.
    import concurrent.futures as cf

    # running_apps() costs a round trip - evaluate it once, not once per browser
    up = running_apps()
    live = [b for b in _BROWSERS if b in up and osa.app_installed(b)]
    if not live:
        return ToolResult(True, content={"tabs": [], "count": 0},
                          display="no browser is running")

    def query(app: str) -> list[dict]:
        tab_prop = "name" if app == "Safari" else "title"
        script = f'''on run argv
  set lim to (item 1 of argv) as integer
  set out to ""
  set n to 0
  tell application "{app}"
    repeat with w in windows
      repeat with t in tabs of w
        if n >= lim then exit repeat
        try
          set out to out & ({tab_prop} of t) & "{osa.FS}" & (URL of t) & "{osa.FS}{app}{osa.RS}"
          set n to n + 1
        end try
      end repeat
      if n >= lim then exit repeat
    end repeat
  end tell
  return out
end run'''
        r = osa.run(script, str(limit), timeout=20)
        return osa.parse_records(r.out, ["title", "url", "browser"]) if r.ok else []

    tabs: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=len(live)) as ex:
        for fut in [ex.submit(query, b) for b in live]:
            try:
                tabs += fut.result(timeout=25)
            except Exception:
                continue
    return ToolResult(True, content={"tabs": tabs[:limit], "count": len(tabs),
                                     "browsers": live},
                      display=f"{len(tabs)} open tab(s) in {', '.join(live)}")


@REGISTRY.tool(
    "open_url",
    "Open a URL in the default browser.",
    {"type": "object", "properties": {"url": {"type": "string"}},
     "required": ["url"]},
    risk=Risk.LOW, category="read", parallel_safe=False)
def open_url(url: str):
    from ..memory.prefs import get_default
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    browser = get_default("browser", "")
    cmd = ["open"] + (["-a", browser] if browser and osa.app_installed(browser) else [])
    subprocess.run(cmd + [url], capture_output=True, timeout=15)
    return ToolResult(True, content={"opened": url, "browser": browser or "default"},
                      display=f"opened {url[:56]}" + (f" in {browser}" if browser else ""))


@REGISTRY.tool(
    "notify",
    "Show a macOS notification. Use to surface something without interrupting.",
    {"type": "object", "properties": {
        "title": {"type": "string"}, "message": {"type": "string"},
        "sound": {"type": "boolean"}},
     "required": ["title", "message"]},
    risk=Risk.LOW, category="write_new")
def notify(title: str, message: str, sound: bool = False):
    script = ('on run argv\n'
              'display notification (item 2 of argv) with title (item 1 of argv)'
              + (' sound name "Ping"' if sound else "") + '\nend run')
    r = osa.run(script, title, message, timeout=12)
    if not r.ok:
        return ToolResult(False, error=r.error)
    return ToolResult(True, content={"notified": title},
                      display=f"notified: {title}")


@REGISTRY.tool(
    "read_clipboard",
    "Read the current clipboard contents (use when the user says 'this', 'what I "
    "just copied'). With history=true, search everything they copied in the last "
    "30 days instead: 'the link I copied this morning', 'that address I copied'. "
    "Put an older item back on the clipboard with restore_id.",
    {"type": "object", "properties": {
        "history": {"type": "boolean", "description": "Search clipboard history"},
        "query": {"type": "string", "description": "Words to look for, or 'link', 'email', 'phone'"},
        "limit": {"type": "integer"},
        "restore_id": {"type": "integer", "description": "Copy this history item again"}},
     "required": []},
    risk=Risk.SAFE, category="read")
def read_clipboard(history: bool = False, query: str = "", limit: int = 10, restore_id: int = 0):
    from ..memory import clips
    if restore_id:
        text = clips.get(restore_id)
        if not text:
            return ToolResult(False, error=f"no clipboard item {restore_id}")
        subprocess.run(["pbcopy"], input=text, text=True, timeout=10)
        return ToolResult(True, content={"restored": restore_id, "text": text[:500],
                                         "verified": True},
                          display="put it back on the clipboard")
    if history or query:
        items = clips.search(query, limit=max(1, min(limit, 30)))
        return ToolResult(True, content={"items": items, "query": query},
                          display=f"{len(items)} clipboard item(s)")
    txt = _sh(["pbpaste"], 10)
    return ToolResult(True, content={"text": txt[:20000], "chars": len(txt)},
                      display=f"clipboard: {len(txt)} chars")


@REGISTRY.tool(
    "write_clipboard",
    "Put text on the clipboard so the user can paste it.",
    {"type": "object", "properties": {"text": {"type": "string"}},
     "required": ["text"]},
    risk=Risk.LOW, category="write_new")
def write_clipboard(text: str):
    try:
        p = subprocess.run(["pbcopy"], input=text, text=True, timeout=10)
    except Exception as e:
        return ToolResult(False, error=str(e))
    return ToolResult(True, content={"copied": len(text)},
                      display=f"copied {len(text)} chars to clipboard")


@REGISTRY.tool(
    "list_shortcuts",
    "List the user's Shortcuts. Shortcuts can automate almost anything on the "
    "Mac and iPhone, so check here when no direct tool fits the request.",
    {"type": "object", "properties": {}, "required": []},
    risk=Risk.SAFE, category="read")
def list_shortcuts():
    out = _sh(["shortcuts", "list"], 20)
    names = [l.strip() for l in out.splitlines() if l.strip()]
    return ToolResult(True, content={"shortcuts": names, "count": len(names)},
                      display=f"{len(names)} shortcut(s)")


@REGISTRY.tool(
    "run_shortcut",
    "Run one of the user's Shortcuts by name, optionally passing text input. "
    "Confirmed first, because a shortcut can do anything.",
    {"type": "object", "properties": {
        "name": {"type": "string"},
        "input": {"type": "string", "description": "Optional text input"}},
     "required": ["name"]},
    risk=Risk.HIGH, category="shortcut", parallel_safe=False,
    assess=lambda a: Verdict(Risk.HIGH,
                             f"runs the shortcut {a.get('name')!r}, which can "
                             f"take any action it was built to take",
                             category="shortcut", needs_confirm=True))
def run_shortcut(name: str, input: str = ""):
    cmd = ["shortcuts", "run", name]
    try:
        if input:
            p = subprocess.run(cmd + ["-i", "-"], input=input, text=True,
                               capture_output=True, timeout=120)
        else:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return ToolResult(False, error=f"shortcut {name!r} timed out")
    if p.returncode != 0:
        return ToolResult(False, error=(p.stderr or "").strip()
                          or f"shortcut failed ({p.returncode})")
    return ToolResult(True, content={"shortcut": name,
                                     "output": (p.stdout or "").strip()[:4000]},
                      display=f"ran shortcut '{name}'")


@REGISTRY.tool(
    "set_focus",
    "Turn Do Not Disturb / a Focus mode on or off.",
    {"type": "object", "properties": {
        "on": {"type": "boolean"},
        "mode": {"type": "string", "description": "Focus name, e.g. 'Do Not Disturb'"}},
     "required": ["on"]},
    risk=Risk.MEDIUM, category="system_pref", parallel_safe=False,
    assess=lambda a: Verdict(Risk.LOW, "changes your Focus mode (reversible)",
                             category="write_new"))
def set_focus(on: bool, mode: str = "Do Not Disturb"):
    # Shortcuts is the only supported way to script Focus on modern macOS.
    verb = "on" if on else "off"
    p = subprocess.run(["shortcuts", "run", f"Set Focus {verb}"],
                       capture_output=True, text=True, timeout=30)
    if p.returncode == 0:
        return ToolResult(True, content={"focus": mode, "on": on},
                          display=f"focus {verb}")
    return ToolResult(False, error=(
        "macOS does not allow Focus to be scripted directly. Create a Shortcut "
        f"named 'Set Focus {verb}' that toggles {mode}, and this will drive it. "
        "You can also toggle it from Control Centre."))


@REGISTRY.tool(
    "lock_screen",
    "Lock the screen immediately.",
    {"type": "object", "properties": {}, "required": []},
    risk=Risk.MEDIUM, category="system_pref", parallel_safe=False,
    assess=lambda a: Verdict(Risk.LOW, "locks the screen (reversible)",
                             category="write_new"))
def lock_screen():
    subprocess.run(["pmset", "displaysleepnow"], capture_output=True, timeout=10)
    return ToolResult(True, content={"locked": True}, display="screen locked")


@REGISTRY.tool(
    "get_weather",
    "Current weather and short forecast for a place. Defaults to the user's "
    "approximate location.",
    {"type": "object", "properties": {
        "location": {"type": "string", "description": "City name; blank = here"}},
     "required": []},
    risk=Risk.SAFE, category="search", slow=True)
def get_weather(location: str = ""):
    import urllib.parse
    import urllib.request
    place = urllib.parse.quote(location or "")
    url = f"https://wttr.in/{place}?format=j1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
    except Exception as e:
        return ToolResult(False, error=f"weather lookup failed: {e}")
    cur = (d.get("current_condition") or [{}])[0]
    area = (d.get("nearest_area") or [{}])[0]
    where = (area.get("areaName", [{}])[0].get("value", "")
             or location or "your location")
    days = []
    for day in (d.get("weather") or [])[:3]:
        days.append({"date": day.get("date"),
                     "min_c": day.get("mintempC"), "max_c": day.get("maxtempC"),
                     "desc": (day.get("hourly", [{}])[4]
                              .get("weatherDesc", [{}])[0].get("value", ""))})
    out = {"location": where,
           "temp_c": cur.get("temp_C"), "feels_like_c": cur.get("FeelsLikeC"),
           "description": (cur.get("weatherDesc") or [{}])[0].get("value", ""),
           "humidity": cur.get("humidity"),
           "wind_kmph": cur.get("windspeedKmph"),
           "forecast": days}
    return ToolResult(True, content=out,
                      display=f"{where}: {out['temp_c']}°C, {out['description']}")


@REGISTRY.tool(
    "active_editor",
    "What the user is looking at right now: the app in front, the file open in "
    "their editor, and the project folder it lives in. Use this when the user "
    "says 'this file', 'the code here', or names a file without a path.",
    {"type": "object", "properties": {}, "required": []},
    risk=Risk.SAFE, category="read")
def active_editor():
    from .. import context
    context.invalidate()          # the user may have switched windows just now
    a = context.active()
    if not a:
        return ToolResult(True, content={"editor": None,
                                         "workspace": str(context.workspace())},
                          display="no editor window detected")
    out = {"app": a.get("app"), "window_title": a.get("title"),
           "file": str(a["file"]) if a.get("file") else None,
           "folder": str(a["folder"]) if a.get("folder") else None,
           "workspace": str(context.workspace())}
    return ToolResult(True, content=out,
                      display=(f"{out['app']}: {out['file']}" if out["file"]
                               else f"{out['app']}: {out['window_title']}"))
