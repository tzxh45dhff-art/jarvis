"""
More of the internet and of time:

    youtube_tool   search, info and transcripts for YouTube videos
    http_request   call any web API (reads are free; anything that sends data asks)
    directions     real route distance and time, and an Apple Maps link
    schedule_task  run a request on a schedule, even while you are away

Guardrails: http_request refuses cloud-metadata and private-network addresses
and asks before it sends anything; scheduled tasks are confirmed when created,
run read-only (any step that would need approval is declined, not taken), and
can be listed and removed.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import plistlib
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path

from ..config import DATA_DIR
from ..safety.policy import Risk, Verdict
from . import web as W
from .registry import REGISTRY, ToolResult

log = logging.getLogger("jarvis.online_plus")

# ================================================================== youtube

_YT_CANDIDATES = ("/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp",
                  "/Library/Frameworks/Python.framework/Versions/3.13/bin/yt-dlp")


def _ytdlp() -> str | None:
    return shutil.which("yt-dlp") or next((p for p in _YT_CANDIDATES if os.path.exists(p)), None)


def _fmt_duration(sec) -> str:
    try:
        s = int(sec)
    except Exception:
        return ""
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_vtt(text: str, timestamps: bool = False) -> str:
    """WebVTT -> clean prose. Auto-captions repeat every line two or three
    times as they scroll; those repeats are collapsed."""
    out: list[str] = []
    last = ""
    cur_ts = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        m = re.match(r"(\d+:)?(\d\d):(\d\d)\.\d+\s+-->", line)
        if m:
            cur_ts = f"{(m.group(1) or '0:').rstrip(':')}:{m.group(2)}:{m.group(3)}"
            continue
        if re.fullmatch(r"\d+", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)             # inline timing / styling tags
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line == last:
            continue
        if last and line.startswith(last):               # scrolling caption grew
            out[-1] = (f"[{cur_ts}] " if timestamps else "") + line
        else:
            out.append((f"[{cur_ts}] " if timestamps else "") + line)
        last = line
    return "\n".join(out) if timestamps else " ".join(out)


@REGISTRY.tool(
    "youtube_tool",
    "YouTube without watching it. action: search (query -> titles, channels, "
    "durations, links), info (title, channel, length, views, date, chapters, "
    "description), transcript (the spoken text, for summaries and questions; "
    "pass offset to read a long one in parts). Works for any public video "
    "that has captions.",
    {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["search", "info", "transcript"]},
        "query": {"type": "string"}, "url": {"type": "string"},
        "timestamps": {"type": "boolean"}, "offset": {"type": "integer"},
        "language": {"type": "string", "description": "Caption language code, default en"}},
     "required": ["action"]},
    risk=Risk.SAFE, category="search", parallel_safe=True, slow=True)
def youtube_tool(action: str, query: str = "", url: str = "", timestamps: bool = False,
                 offset: int = 0, language: str = "en"):
    yt = _ytdlp()
    if not yt:
        return ToolResult(False, error="yt-dlp is not installed (brew install yt-dlp)")
    action = (action or "").lower()
    env = dict(os.environ, PATH=os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin")
    try:
        if action == "search":
            q = (query or "").strip()
            if not q:
                return ToolResult(False, error="what should I search for?")
            r = subprocess.run([yt, f"ytsearch8:{q}", "--flat-playlist", "-J", "--no-warnings"],
                               capture_output=True, text=True, timeout=60, env=env)
            j = json.loads(r.stdout or "{}")
            rows = [{"title": e.get("title"), "channel": e.get("channel") or e.get("uploader"),
                     "duration": _fmt_duration(e.get("duration")),
                     "views": e.get("view_count"), "url": e.get("url") or
                     f"https://www.youtube.com/watch?v={e.get('id')}"} for e in j.get("entries", [])]
            if not rows:
                return ToolResult(False, error="no videos found")
            return ToolResult(True, content={"query": q, "videos": rows},
                              display=f"{len(rows)} videos for {q[:40]}")
        u = (url or "").strip()
        if not u:
            return ToolResult(False, error="give the video's url")
        if action == "info":
            r = subprocess.run([yt, "-J", "--no-playlist", "--skip-download", "--no-warnings", u],
                               capture_output=True, text=True, timeout=60, env=env)
            if r.returncode != 0 or not r.stdout.strip():
                return ToolResult(False, error=(r.stderr.strip().splitlines() or ["video not available"])[-1][:200])
            j = json.loads(r.stdout)
            return ToolResult(True, content={
                "title": j.get("title"), "channel": j.get("channel") or j.get("uploader"),
                "duration": _fmt_duration(j.get("duration")), "views": j.get("view_count"),
                "uploaded": j.get("upload_date"), "likes": j.get("like_count"),
                "chapters": [{"start": _fmt_duration(c.get("start_time")), "title": c.get("title")}
                             for c in (j.get("chapters") or [])][:30],
                "description": (j.get("description") or "")[:900],
                "has_captions": bool(j.get("subtitles") or j.get("automatic_captions")),
                "url": j.get("webpage_url") or u},
                display=f"{j.get('title', '')[:60]}")
        if action == "transcript":
            lang = (language or "en").lower()[:5]
            with tempfile.TemporaryDirectory() as td:
                r = subprocess.run(
                    [yt, "--skip-download", "--write-subs", "--write-auto-subs",
                     "--sub-langs", f"{lang}.*,{lang}", "--sub-format", "vtt/best",
                     "--no-playlist", "--no-warnings", "-o", os.path.join(td, "%(id)s"), u],
                    capture_output=True, text=True, timeout=120, env=env)
                vtts = sorted(Path(td).glob("*.vtt"))
                if not vtts:
                    err = (r.stderr.strip().splitlines() or [""])[-1][:160]
                    return ToolResult(False, error="this video has no captions I can read"
                                                   + (f" ({err})" if err else ""))
                # prefer manually written captions over automatic ones
                vtts.sort(key=lambda p: (".auto" in p.name, len(p.name)))
                text = parse_vtt(vtts[0].read_text(errors="replace"), bool(timestamps))
            if not text.strip():
                return ToolResult(False, error="the captions were empty")
            size = 9000
            start = max(0, int(offset or 0))
            part = text[start:start + size]
            out = {"transcript": part, "total_chars": len(text), "offset": start, "url": u}
            if start + size < len(text):
                out["more"] = True
                out["next_offset"] = start + size
                out["note"] = f"showing {start:,}-{start + len(part):,} of {len(text):,}; call again with offset={start + size}"
            return ToolResult(True, content=out, display=f"transcript: {len(text):,} characters")
    except subprocess.TimeoutExpired:
        return ToolResult(False, error="YouTube took too long to answer")
    except Exception as e:                        # noqa: BLE001
        return ToolResult(False, error=f"YouTube lookup failed: {e}"[:250])
    return ToolResult(False, error=f"unknown action {action!r}")


# ================================================================== http

_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _effective_ip(ip):
    """The address a packet really reaches. Mobile and IPv6-only networks
    resolve ordinary sites to NAT64 (64:ff9b::/96) or IPv4-mapped addresses,
    which look 'reserved' but are just the public IPv4 address in disguise."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _host_blocked(host: str) -> str | None:
    """Why a host must not be called, or None. Loopback is allowed (local dev
    servers are a normal use); cloud-metadata and private LAN ranges are not."""
    h = (host or "").strip("[]").lower()
    if not h:
        return "no host"
    if h in ("localhost", "127.0.0.1", "::1"):
        return None
    try:
        infos = socket.getaddrinfo(h, None)
    except OSError:
        return None                                  # let the request report DNS failure
    for fam, _, _, _, sa in infos:
        ip = _effective_ip(ipaddress.ip_address(sa[0].split("%")[0]))
        if ip.is_loopback:
            continue
        if ip.is_link_local or ip.is_private or ip.is_multicast or ip.is_reserved \
                or ip.is_unspecified:
            return f"{h} resolves to a private or internal address ({ip})"
    return None


def _assess_http(args: dict) -> Verdict:
    method = str(args.get("method", "GET")).upper()
    host = urllib.parse.urlsplit(str(args.get("url", ""))).hostname or ""
    bad = _host_blocked(host)
    if bad:
        return Verdict(Risk.FORBIDDEN, f"refusing: {bad}", category="network")
    if method in ("GET", "HEAD", "OPTIONS"):
        return Verdict(Risk.SAFE, "reads from the web", category="read")
    return Verdict(Risk.HIGH, f"sends data to {host} with {method}",
                   category="network_write", needs_confirm=True,
                   detail=str(args.get("url", ""))[:120])


@REGISTRY.tool(
    "http_request",
    "Call a web API or URL directly: method, headers, query params, JSON or "
    "text body. Returns status, selected headers and the parsed JSON (or "
    "text). GET and HEAD run freely; POST/PUT/PATCH/DELETE ask the user "
    "first. Private-network and cloud-metadata addresses are refused.",
    {"type": "object", "properties": {
        "url": {"type": "string"},
        "method": {"type": "string", "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]},
        "headers": {"type": "object"}, "params": {"type": "object"},
        "json": {"type": "object"}, "body": {"type": "string"},
        "timeout": {"type": "integer"}},
     "required": ["url"]},
    risk=Risk.MEDIUM, category="network", parallel_safe=True, slow=True, assess=_assess_http)
def http_request(url: str, method: str = "GET", headers: dict | None = None,
                 params: dict | None = None, json: dict | None = None, body: str = "",
                 timeout: int = 30):
    import requests
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    method = (method or "GET").upper()
    t0 = time.time()
    try:
        r = requests.request(method, u, headers={"User-Agent": W._UA, **(headers or {})},
                             params=params or None, json=json if json is not None else None,
                             data=body.encode() if body and json is None else None,
                             timeout=max(3, min(int(timeout or 30), 60)), allow_redirects=True)
    except Exception as e:                        # noqa: BLE001
        return ToolResult(False, error=f"request failed: {type(e).__name__}: {str(e)[:160]}")
    # a redirect must not carry us somewhere forbidden either
    final_host = urllib.parse.urlsplit(r.url).hostname or ""
    bad = _host_blocked(final_host)
    if bad:
        return ToolResult(False, error=f"the request was redirected to a blocked address: {bad}")
    ctype = r.headers.get("Content-Type", "")
    out: dict = {"status": r.status_code, "ok": r.ok, "url": r.url,
                 "content_type": ctype.split(";")[0], "bytes": len(r.content),
                 "seconds": round(time.time() - t0, 2),
                 "headers": {k: v for k, v in r.headers.items()
                             if k.lower() in ("content-type", "content-length", "location", "etag",
                                              "last-modified", "retry-after", "x-ratelimit-remaining",
                                              "server", "cache-control")}}
    if method == "HEAD":
        return ToolResult(r.ok, content=out, error="" if r.ok else f"HTTP {r.status_code}",
                          display=f"HEAD {urllib.parse.urlsplit(r.url).netloc}: {r.status_code}")
    if "json" in ctype:
        try:
            data = r.json()
            text = __import__("json").dumps(data, ensure_ascii=False)
            out["json"] = data if len(text) <= 9000 else None
            if out["json"] is None:
                out["json_preview"] = text[:6000]
                out["note"] = f"JSON is {len(text):,} characters; showing the first 6,000"
        except Exception:
            out["text"] = r.text[:6000]
    elif ctype.startswith("text/") or "xml" in ctype or "javascript" in ctype:
        out["text"] = r.text[:6000]
        if len(r.text) > 6000:
            out["note"] = f"text is {len(r.text):,} characters; showing the first 6,000"
    else:
        out["note"] = "binary response; use download_file to save it"
    return ToolResult(r.ok, content=out, error="" if r.ok else f"HTTP {r.status_code}",
                      display=f"{method} {urllib.parse.urlsplit(r.url).netloc}: {r.status_code}")


# ================================================================== directions

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_ROUTERS = {"driving": "https://routing.openstreetmap.de/routed-car/route/v1/driving",
            "walking": "https://routing.openstreetmap.de/routed-foot/route/v1/foot",
            "cycling": "https://routing.openstreetmap.de/routed-bike/route/v1/bike"}
_GEO_CACHE = W.TTLCache(64)


def _geocode(place: str) -> dict | None:
    key = place.lower().strip()
    hit = _GEO_CACHE.get(key)
    if hit is not None:
        return hit
    import requests
    r = requests.get(_NOMINATIM, params={"q": place, "format": "json", "limit": 1},
                     headers={"User-Agent": "jarvis-local/1.0 (personal assistant)"}, timeout=10)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return None
    d = {"lat": float(rows[0]["lat"]), "lon": float(rows[0]["lon"]),
         "name": rows[0].get("display_name", place)}
    _GEO_CACHE.put(key, d, 86400)
    return d


def _here() -> dict | None:
    import requests
    try:
        j = requests.get("https://ipinfo.io/json", timeout=6).json()
        lat, lon = [float(x) for x in j["loc"].split(",")]
        return {"lat": lat, "lon": lon, "name": f"{j.get('city')}, {j.get('region')} (approximate)"}
    except Exception:
        return None


def _hm(sec: float) -> str:
    m = int(round(sec / 60))
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


@REGISTRY.tool(
    "directions",
    "Route distance and travel time between two places, with an Apple Maps "
    "link. mode: driving (default) | walking | cycling. Leave origin empty to "
    "start from here (approximate, from the network). open=true also opens it "
    "in Maps.",
    {"type": "object", "properties": {
        "destination": {"type": "string"}, "origin": {"type": "string"},
        "mode": {"type": "string", "enum": ["driving", "walking", "cycling"]},
        "open": {"type": "boolean"}},
     "required": ["destination"]},
    risk=Risk.SAFE, category="search", parallel_safe=True, slow=True)
def directions(destination: str, origin: str = "", mode: str = "driving", open: bool = False):
    import requests
    mode = (mode or "driving").lower()
    if mode not in _ROUTERS:
        return ToolResult(False, error="mode: driving, walking or cycling")
    try:
        dst = _geocode(destination)
        if dst is None:
            return ToolResult(False, error=f"could not find {destination!r} on the map")
        src = _geocode(origin) if origin else _here()
        if src is None:
            return ToolResult(False, error=f"could not find {origin!r}" if origin
                              else "could not work out where you are; give an origin")
        url = f"{_ROUTERS[mode]}/{src['lon']},{src['lat']};{dst['lon']},{dst['lat']}"
        r = requests.get(url, params={"overview": "false"}, timeout=15,
                         headers={"User-Agent": "jarvis-local/1.0"})
        r.raise_for_status()
        route = r.json()["routes"][0]
    except Exception as e:                        # noqa: BLE001
        return ToolResult(False, error=f"routing failed: {type(e).__name__}: {str(e)[:140]}")
    km = route["distance"] / 1000
    # OSRM's foot/bike servers return car-like times for some regions; use
    # realistic speeds for people on foot or bikes.
    secs = {"walking": km / 4.8 * 3600, "cycling": km / 15 * 3600}.get(mode, route["duration"])
    flag = {"driving": "d", "walking": "w", "cycling": "w"}[mode]
    maps = ("https://maps.apple.com/?" + urllib.parse.urlencode({
        "saddr": f"{src['lat']},{src['lon']}", "daddr": f"{dst['lat']},{dst['lon']}", "dirflg": flag}))
    if open:
        subprocess.Popen(["/usr/bin/open", maps])
    return ToolResult(True, content={
        "from": src["name"][:90], "to": dst["name"][:90], "mode": mode,
        "distance_km": round(km, 1), "travel_time": _hm(secs),
        "note": "typical time without live traffic" if mode == "driving" else "estimated at walking/cycling pace",
        "maps_url": maps}, display=f"{km:.1f} km, about {_hm(secs)} by {mode}")


# ================================================================== scheduler

TASK_DIR = DATA_DIR / "tasks"
AGENTS = Path.home() / "Library" / "LaunchAgents"
LABEL_PREFIX = "com.example.jarvis.task."


def _jarvis_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_plist(task_id: str, hour: int, minute: int, weekdays: list[int] | None,
                one_shot_date: tuple[int, int] | None = None) -> dict:
    """The launchd job for one scheduled task."""
    py = _jarvis_root() / ".venv" / "bin" / "python"
    intervals = []
    if one_shot_date:
        mo, dy = one_shot_date
        intervals.append({"Month": mo, "Day": dy, "Hour": hour, "Minute": minute})
    elif weekdays:
        for d in weekdays:
            intervals.append({"Weekday": d, "Hour": hour, "Minute": minute})
    else:
        intervals.append({"Hour": hour, "Minute": minute})
    return {
        "Label": LABEL_PREFIX + task_id,
        "ProgramArguments": [str(py if py.exists() else "/usr/bin/python3"), "-W", "ignore",
                             "-m", "jarvis.scheduled_runner", task_id],
        "EnvironmentVariables": {"PYTHONPATH": str(_jarvis_root()),
                                 "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "WorkingDirectory": str(Path.home()),
        "StartCalendarInterval": intervals if len(intervals) > 1 else intervals[0],
        "StandardOutPath": str(TASK_DIR / f"{task_id}.log"),
        "StandardErrorPath": str(TASK_DIR / f"{task_id}.log"),
        "LimitLoadToSessionType": "Aqua",
    }


_DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def parse_when(when: str) -> dict | None:
    """'every day at 8:30', 'weekdays at 9am', 'every monday 07:00', 'at 6pm',
    'tomorrow at 7am' -> {hour, minute, weekdays|None, date|None}."""
    t = (when or "").lower().strip()
    if not t:
        return None
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t.replace("at ", " "))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) == "pm" and hour < 12:
        hour += 12
    if m.group(3) == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    weekdays = None
    if "weekday" in t:
        weekdays = [1, 2, 3, 4, 5]
    elif "weekend" in t:
        weekdays = [0, 6]
    else:
        found = sorted({v for k, v in _DAYS.items() if re.search(rf"\b{k}", t)})
        weekdays = found or None
    date = None
    if re.search(r"\btomorrow\b", t) or re.search(r"\btoday\b", t):
        import datetime as dt
        d = dt.date.today() + dt.timedelta(days=1 if "tomorrow" in t else 0)
        date = (d.month, d.day)
    recurring = bool(re.search(r"\bevery\b|\bdaily\b|\bweekday|\bweekend|\beach\b", t)) or \
        (weekdays is not None and date is None)
    if date is None and not recurring and weekdays is None:
        # a bare "at 6pm": the next time that clock reads 6pm
        import datetime as dt
        now = dt.datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        date = (target.month, target.day)
    return {"hour": hour, "minute": minute, "weekdays": weekdays, "date": date,
            "recurring": recurring and date is None}


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=20)


def _assess_schedule(args: dict) -> Verdict:
    if str(args.get("action", "")).lower() == "add":
        return Verdict(Risk.MEDIUM, "creates a background job that runs on its own",
                       category="persistent", needs_confirm=True,
                       detail=f"{args.get('when', '')}: {str(args.get('prompt', ''))[:80]}")
    return Verdict(Risk.SAFE, "lists or removes scheduled tasks", category="read")


@REGISTRY.tool(
    "schedule_task",
    "Run a request on a schedule, unattended, with the result as a macOS "
    "notification. action: add (when, prompt) | list | remove (id). 'when' "
    "like 'every day at 8:30', 'weekdays at 9am', 'every monday 7am', "
    "'tomorrow at 7am', 'at 6pm'. Scheduled tasks run unattended and keep to "
    "looking things up: anything that would need approval (sending, "
    "deleting, buying) is declined, so use this for briefings, checks and "
    "reminders-with-substance (weather and calendar summary, a price check, "
    "news on a topic).",
    {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["add", "list", "remove"]},
        "when": {"type": "string"}, "prompt": {"type": "string"},
        "id": {"type": "string"}},
     "required": ["action"]},
    risk=Risk.MEDIUM, category="persistent", parallel_safe=False, assess=_assess_schedule)
def schedule_task(action: str, when: str = "", prompt: str = "", id: str = ""):
    action = (action or "").lower()
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    if action == "list":
        rows = []
        for f in sorted(TASK_DIR.glob("*.json")):
            try:
                j = json.loads(f.read_text())
            except Exception:
                continue
            last = TASK_DIR / f"{j['id']}.last.txt"
            rows.append({"id": j["id"], "when": j["when"], "prompt": j["prompt"],
                         "last_result": last.read_text()[:200] if last.exists() else None})
        return ToolResult(True, content={"tasks": rows}, display=f"{len(rows)} scheduled task(s)")
    if action == "remove":
        meta = TASK_DIR / f"{id}.json"
        if not id or not meta.exists():
            return ToolResult(False, error="no such task")
        plist = AGENTS / f"{LABEL_PREFIX}{id}.plist"
        _launchctl("bootout", f"gui/{os.getuid()}", str(plist))
        for p in (plist, meta, TASK_DIR / f"{id}.last.txt", TASK_DIR / f"{id}.log"):
            p.unlink(missing_ok=True)
        return ToolResult(True, content={"removed": id}, display=f"removed task {id}")
    if action != "add":
        return ToolResult(False, error=f"unknown action {action!r}")
    p = (prompt or "").strip()
    if len(p) < 4:
        return ToolResult(False, error="say what the task should do")
    sched = parse_when(when)
    if sched is None:
        return ToolResult(False, error="say when: 'every day at 8:30', 'weekdays at 9am', "
                                       "'tomorrow at 7am', 'at 6pm'")
    if len(list(TASK_DIR.glob("*.json"))) >= 12:
        return ToolResult(False, error="that is already 12 scheduled tasks; remove one first")
    tid = uuid.uuid4().hex[:8]
    (TASK_DIR / f"{tid}.json").write_text(json.dumps({
        "id": tid, "when": when.strip(), "prompt": p, "created": time.time(),
        "one_shot": not sched["recurring"]}))
    plist = build_plist(tid, sched["hour"], sched["minute"], sched["weekdays"], sched["date"])
    path = AGENTS / f"{LABEL_PREFIX}{tid}.plist"
    with open(path, "wb") as fh:
        plistlib.dump(plist, fh)
    r = _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    if r.returncode != 0:
        (TASK_DIR / f"{tid}.json").unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        return ToolResult(False, error=f"could not schedule it: {(r.stderr or r.stdout).strip()[:160]}")
    return ToolResult(True, content={
        "id": tid, "when": when.strip(), "prompt": p,
        "repeats": sched["recurring"], "verified": True,
        "note": "runs read-only and shows the result as a notification; "
                "remove with schedule_task(action='remove', id=...)"},
        display=f"scheduled: {when.strip()} - {p[:50]}")
