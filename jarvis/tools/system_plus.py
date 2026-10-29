"""
The machine itself: timers, running programs, the network, disk space, windows.

    timer           set / list / cancel timers that outlive the engine
    processes       what is running, what is hogging the CPU, quit something
    network_info    status, public IP, ping, is-this-site-down, speed test
    disk_usage      what is taking the space under a folder
    window_manage   list, move, resize, tile and minimise windows

Guardrails: ending a process asks first and refuses system-critical ones and
JARVIS itself; nothing here changes files.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import subprocess
import time
import urllib.parse
import uuid
from pathlib import Path

from ..config import DATA_DIR
from ..safety.policy import Risk, Verdict
from .registry import REGISTRY, ToolResult

log = logging.getLogger("jarvis.system_plus")


def _sh(cmd: list[str], timeout: int = 20) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        return (r.stdout or "").strip()
    except Exception:
        return ""


# ================================================================== timers

_TIMERS = DATA_DIR / "timers.json"


def _load_timers() -> list[dict]:
    try:
        return json.loads(_TIMERS.read_text())
    except Exception:
        return []


def _save_timers(rows: list[dict]) -> None:
    _TIMERS.parent.mkdir(parents=True, exist_ok=True)
    _TIMERS.write_text(json.dumps(rows, indent=1))


def _alive(pid: int) -> bool:
    """Running, as opposed to gone or a zombie waiting for its parent to reap
    it (which `kill -0` still reports as alive)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    stat = _sh(["/bin/ps", "-o", "stat=", "-p", str(pid)], 5)
    return bool(stat) and not stat.startswith("Z")


def parse_duration(text: str) -> float | None:
    """'90', '90s', '5 min', '1h30m', '1 hour 15 minutes', '1:30:00' -> seconds."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return float(t)
    m = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", t)
    if m:
        a, b, c = m.groups()
        return int(a) * 3600 + int(b) * 60 + int(c or 0) if c is not None else int(a) * 60 + int(b)
    total, found = 0.0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])", t):
        n = float(num)
        u = unit[0]
        total += n * (3600 if u == "h" else 60 if u == "m" else 1)
        found = True
    return total if found else None


@REGISTRY.tool(
    "timer",
    "Timers and alarms that still fire if JARVIS restarts. action: set "
    "(duration like '10 minutes' / '1h30m' / seconds, OR at='17:30' / '5pm' for "
    "a clock time; optional label and speak=true to say it aloud), list, "
    "cancel (id or 'all'). A notification with a sound appears when it ends.",
    {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["set", "list", "cancel"]},
        "duration": {"type": "string"}, "at": {"type": "string"},
        "label": {"type": "string"}, "speak": {"type": "boolean"},
        "id": {"type": "string"}},
     "required": ["action"]},
    risk=Risk.LOW, category="write_new", parallel_safe=False)
def timer(action: str, duration: str = "", at: str = "", label: str = "",
          speak: bool = False, id: str = ""):
    action = (action or "").lower()
    rows = [r for r in _load_timers() if _alive(r.get("pid", 0)) and r.get("due", 0) > time.time() - 5]
    if action == "list":
        _save_timers(rows)
        return ToolResult(True, content={"timers": [
            {"id": r["id"], "label": r["label"], "in_seconds": max(0, int(r["due"] - time.time())),
             "ends_at": time.strftime("%H:%M:%S", time.localtime(r["due"]))} for r in rows]},
            display=f"{len(rows)} timer(s)")
    if action == "cancel":
        hit = [r for r in rows if id in ("all", "") or r["id"] == id]
        if not hit:
            return ToolResult(False, error="no such timer")
        for r in hit:
            try:
                os.kill(r["pid"], signal.SIGTERM)
            except OSError:
                pass
        _save_timers([r for r in rows if r not in hit])
        return ToolResult(True, content={"cancelled": [r["id"] for r in hit]},
                          display=f"cancelled {len(hit)} timer(s)")
    if action != "set":
        return ToolResult(False, error=f"unknown action {action!r}")
    secs = None
    if duration:
        secs = parse_duration(duration)
    elif at:
        try:
            import dateparser
            d = dateparser.parse(at, settings={"PREFER_DATES_FROM": "future",
                                               "RETURN_AS_TIMEZONE_AWARE": False})
            if d is not None:
                secs = (d - __import__("datetime").datetime.now()).total_seconds()
                if secs < 0:
                    secs += 86400
        except Exception:
            secs = None
    if secs is None or secs < 1:
        return ToolResult(False, error="say how long ('10 minutes') or when ('at 5pm')")
    if secs > 7 * 86400:
        return ToolResult(False, error="timers longer than a week should be calendar events")
    tid = uuid.uuid4().hex[:6]
    text = (label or "Timer").strip()[:80]
    script = ('sleep "$1"; /usr/bin/osascript -e '
              '\'display notification (system attribute "JL") with title "JARVIS" '
              'sound name "Glass"\'; if [ "$2" = "1" ]; then /usr/bin/say "$JL"; fi')
    env = dict(os.environ, JL=text if text != "Timer" else "Your timer is done")
    p = subprocess.Popen(["/bin/sh", "-c", script, "sh", str(int(secs)), "1" if speak else "0"],
                         env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    due = time.time() + secs
    rows.append({"id": tid, "label": text, "due": due, "pid": p.pid})
    _save_timers(rows)
    return ToolResult(True, content={
        "id": tid, "label": text, "seconds": int(secs),
        "ends_at": time.strftime("%H:%M:%S", time.localtime(due)), "verified": _alive(p.pid)},
        display=f"timer set: {text} ({int(secs // 60)} min {int(secs % 60)} s)" if secs >= 60
        else f"timer set: {text} ({int(secs)} s)")


# ================================================================== processes

_PROTECTED = {"launchd", "kernel_task", "windowserver", "loginwindow", "coreaudiod",
              "mds", "powerd", "configd", "logd", "systemuiserver", "dock", "finder",
              "distnoted", "cfprefsd", "securityd", "opendirectoryd", "airportd",
              "bluetoothd", "notifyd", "usernoted", "universalaccessd"}


def _ps() -> list[dict]:
    out = _sh(["/bin/ps", "-Ao", "pid=,ppid=,user=,pcpu=,pmem=,rss=,etime=,comm="], 15)
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        pid, ppid, user, cpu, mem, rss, et, comm = parts
        try:
            rows.append({"pid": int(pid), "ppid": int(ppid), "user": user,
                         "cpu": float(cpu), "mem_pct": float(mem),
                         "mem_mb": round(int(rss) / 1024, 1), "running": et,
                         "name": os.path.basename(comm) if not comm.endswith(".app") else comm,
                         "path": comm})
        except ValueError:
            continue
    return rows


def _friendly(row: dict) -> str:
    m = re.search(r"/([^/]+)\.app/", row["path"])
    return m.group(1) if m else row["name"]


def _assess_processes(args: dict) -> Verdict:
    if str(args.get("action", "")).lower() == "quit":
        return Verdict(Risk.HIGH, "ends a running program, losing unsaved work in it",
                       category="process", needs_confirm=True)
    return Verdict(Risk.SAFE, "reads the process list", category="read")


@REGISTRY.tool(
    "processes",
    "Look at what is running and, when asked, end a program. action: top "
    "(biggest CPU or memory users; sort='cpu'|'memory', limit), find (name "
    "contains), quit (pid, or name when exactly one matches; force=true for a "
    "stuck one). Quitting always asks the user and refuses system-critical "
    "processes.",
    {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["top", "find", "quit"]},
        "name": {"type": "string"}, "pid": {"type": "integer"},
        "sort": {"type": "string", "enum": ["cpu", "memory"]},
        "limit": {"type": "integer"}, "force": {"type": "boolean"}},
     "required": ["action"]},
    risk=Risk.MEDIUM, category="process", parallel_safe=False, assess=_assess_processes)
def processes(action: str, name: str = "", pid: int = 0, sort: str = "cpu",
              limit: int = 10, force: bool = False):
    action = (action or "").lower()
    me = os.getuid()
    import pwd
    try:
        user = pwd.getpwuid(me).pw_name
    except KeyError:
        user = ""
    rows = _ps()
    if action in ("top", "find"):
        if action == "find":
            q = name.lower().strip()
            if not q:
                return ToolResult(False, error="which program?")
            rows = [r for r in rows if q in r["path"].lower() or q in _friendly(r).lower()]
        key = "mem_mb" if sort.startswith("mem") else "cpu"
        rows = sorted(rows, key=lambda r: -r[key])[:max(1, min(limit or 10, 40))]
        return ToolResult(True, content={"processes": [
            {"pid": r["pid"], "name": _friendly(r), "cpu_percent": r["cpu"],
             "memory_mb": r["mem_mb"], "running_for": r["running"],
             "mine": r["user"] == user} for r in rows]},
            display=f"{len(rows)} process(es)")
    if action != "quit":
        return ToolResult(False, error=f"unknown action {action!r}")
    target = None
    if pid:
        target = next((r for r in rows if r["pid"] == pid), None)
    elif name:
        q = name.lower().strip()
        hits = [r for r in rows if r["user"] == user and
                (q == _friendly(r).lower() or q == r["name"].lower())] or \
               [r for r in rows if r["user"] == user and q in _friendly(r).lower()]
        # one app has many helper processes; the app itself is the shallowest
        if hits:
            main = min(hits, key=lambda r: len(r["path"]))
            same_app = {_friendly(h) for h in hits}
            if len(same_app) > 1:
                return ToolResult(False, error="several programs match: " + ", ".join(sorted(same_app)[:6])
                                               + " - use a pid or a more exact name")
            target = main
    if target is None:
        return ToolResult(False, error="no matching process of yours")
    if target["pid"] <= 1 or target["user"] != user:
        return ToolResult(False, error=f"refusing: {_friendly(target)} is not one of your own programs")
    low = {_friendly(target).lower(), target["name"].lower()}
    if low & _PROTECTED:
        return ToolResult(False, error=f"refusing: {_friendly(target)} is a system-critical process")
    if target["pid"] in (os.getpid(), os.getppid()):
        return ToolResult(False, error="refusing: that is JARVIS itself")
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(target["pid"], sig)
    except OSError as e:
        return ToolResult(False, error=f"could not end it: {e}")
    for _ in range(25):
        time.sleep(0.2)
        if not _alive(target["pid"]):
            break
    gone = not _alive(target["pid"])
    return ToolResult(gone, content={"pid": target["pid"], "name": _friendly(target),
                                     "ended": gone, "signal": "SIGKILL" if force else "SIGTERM",
                                     "verified": gone},
                      error="" if gone else (f"{_friendly(target)} did not exit; "
                                             f"ask the user before using force=true"),
                      display=f"ended {_friendly(target)}" if gone else f"{_friendly(target)} still running")


# ================================================================== network

def _ping(host: str, count: int = 4) -> dict:
    out = _sh(["/sbin/ping", "-c", str(count), "-t", "8", host], 20)
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/", out)
    return {"host": host, "reachable": bool(rtt),
            "packet_loss_percent": float(loss.group(1)) if loss else 100.0,
            "avg_ms": float(rtt.group(2)) if rtt else None,
            "min_ms": float(rtt.group(1)) if rtt else None,
            "max_ms": float(rtt.group(3)) if rtt else None}


@REGISTRY.tool(
    "network_info",
    "Network diagnostics. action: status (Wi-Fi name, local and public IP, "
    "connectivity), ping (host), site (is a website up? status code and "
    "response time for a URL), dns (resolve a hostname), speed (full "
    "download/upload speed test, ~20 s).",
    {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["status", "ping", "site", "dns", "speed"]},
        "host": {"type": "string"}, "url": {"type": "string"}},
     "required": ["action"]},
    risk=Risk.SAFE, category="read", parallel_safe=True, slow=True)
def network_info(action: str, host: str = "", url: str = ""):
    action = (action or "").lower()
    if action == "ping":
        h = (host or url or "").strip().replace("https://", "").replace("http://", "").split("/")[0]
        if not h:
            return ToolResult(False, error="which host?")
        r = _ping(h)
        return ToolResult(True, content=r, display=f"ping {h}: "
                          + (f"{r['avg_ms']} ms" if r["reachable"] else "no reply"))
    if action == "dns":
        h = (host or "").strip()
        if not h:
            return ToolResult(False, error="which hostname?")
        try:
            t0 = time.time()
            infos = socket.getaddrinfo(h, None)
            addrs = sorted({i[4][0] for i in infos})
            return ToolResult(True, content={"host": h, "addresses": addrs[:8],
                                             "ms": round((time.time() - t0) * 1000)},
                              display=f"{h} -> {addrs[0]}")
        except OSError as e:
            return ToolResult(False, error=f"could not resolve {h}: {e}")
    if action == "site":
        u = (url or host or "").strip()
        if not u:
            return ToolResult(False, error="which site?")
        if not u.startswith("http"):
            u = "https://" + u
        import requests
        t0 = time.time()
        try:
            r = requests.get(u, timeout=12, allow_redirects=True, stream=True,
                             headers={"User-Agent": "Mozilla/5.0"})
            ms = round((time.time() - t0) * 1000)
            r.close()
            up = r.status_code < 500
            return ToolResult(True, content={
                "url": u, "up": up, "status": r.status_code, "response_ms": ms,
                "final_url": r.url, "server": r.headers.get("Server", "")},
                display=f"{urllib.parse.urlsplit(u).netloc}: "
                        f"{'up' if up else 'ERROR'} ({r.status_code}, {ms} ms)")
        except Exception as e:                    # noqa: BLE001
            msg = type(e).__name__
            return ToolResult(True, content={"url": u, "up": False, "error": msg,
                                             "from_here": True,
                                             "note": "unreachable from this Mac; the site may "
                                                     "still be up for others"},
                              display=f"{urllib.parse.urlsplit(u).netloc}: unreachable ({msg})")
    if action == "speed":
        nq = "/usr/bin/networkQuality"
        if not os.path.exists(nq):
            return ToolResult(False, error="networkQuality is not available on this macOS")
        out = _sh([nq, "-c", "-M", "15"], 60)
        try:
            j = json.loads(out)
        except Exception:
            return ToolResult(False, error="the speed test did not complete (is the network up?)")
        dl = j.get("dl_throughput", 0) / 1e6
        ul = j.get("ul_throughput", 0) / 1e6
        return ToolResult(True, content={
            "download_mbps": round(dl, 1), "upload_mbps": round(ul, 1),
            "responsiveness_rpm": j.get("responsiveness"), "base_rtt_ms": j.get("base_rtt"),
            "interface": j.get("interface_name")},
            display=f"speed: {dl:.0f} Mbps down, {ul:.0f} Mbps up")
    if action == "status":
        out: dict = {}
        ssid = _sh(["/usr/sbin/networksetup", "-getairportnetwork", "en0"], 8)
        m = re.search(r"Current Wi-Fi Network:\s*(.+)", ssid)
        out["wifi"] = m.group(1).strip() if m else ("(name hidden by macOS)" if "associated" in ssid.lower() else "not connected")
        out["local_ip"] = _sh(["/usr/sbin/ipconfig", "getifaddr", "en0"], 5) or _sh(["/usr/sbin/ipconfig", "getifaddr", "en1"], 5)
        try:
            import requests
            j = requests.get("https://ipinfo.io/json", timeout=6).json()
            out.update({"public_ip": j.get("ip"), "city": j.get("city"), "region": j.get("region"),
                        "country": j.get("country"), "provider": j.get("org")})
            out["internet"] = True
        except Exception:
            out["internet"] = _ping("1.1.1.1", 2)["reachable"]
        return ToolResult(True, content=out,
                          display=f"{out['wifi']} - internet {'ok' if out.get('internet') else 'down'}")
    return ToolResult(False, error=f"unknown action {action!r}")


# ================================================================== disk

@REGISTRY.tool(
    "disk_usage",
    "What is taking space. Without a path: free space on the startup disk and "
    "the biggest folders in the home directory. With a path: its biggest "
    "children. Optionally list individual files above min_mb.",
    {"type": "object", "properties": {
        "path": {"type": "string"}, "min_mb": {"type": "integer",
                                               "description": "List single files at least this big"},
        "limit": {"type": "integer"}},
     "required": []},
    risk=Risk.SAFE, category="read", parallel_safe=True, slow=True)
def disk_usage(path: str = "", min_mb: int = 0, limit: int = 12):
    from .fs import _p
    base = _p(path) if path else Path.home()
    if not base.exists():
        return ToolResult(False, error=f"not found: {base}")
    out: dict = {"path": str(base)}
    try:
        st = os.statvfs("/")
        out["startup_disk"] = {"free_gb": round(st.f_bavail * st.f_frsize / 1e9, 1),
                               "total_gb": round(st.f_blocks * st.f_frsize / 1e9, 1)}
    except OSError:
        pass
    limit = max(1, min(int(limit or 12), 40))
    if min_mb:
        r = subprocess.run(["/usr/bin/find", str(base), "-xdev", "-type", "f",
                            "-size", f"+{int(min_mb)}M", "-not", "-path", "*/Library/*",
                            "-not", "-path", "*/.Trash/*"],
                           capture_output=True, text=True, timeout=90, errors="replace")
        files = []
        for line in r.stdout.splitlines()[:600]:
            try:
                files.append((os.path.getsize(line), line))
            except OSError:
                continue
        files.sort(reverse=True)
        out["large_files"] = [{"path": p, "mb": round(s / 1e6)} for s, p in files[:limit]]
        out["note"] = "Library folders are skipped"
        return ToolResult(True, content=out, display=f"{len(files)} files over {min_mb} MB")
    try:
        r = subprocess.run(["/usr/bin/du", "-k", "-d", "1", str(base)], capture_output=True,
                           text=True, timeout=90, errors="replace")
    except subprocess.TimeoutExpired:
        return ToolResult(False, error="that folder is too big to scan quickly; try a subfolder")
    rows = []
    for line in r.stdout.splitlines():
        m = re.match(r"(\d+)\s+(.+)", line)
        if m and m.group(2) != str(base):
            rows.append((int(m.group(1)), m.group(2)))
    rows.sort(reverse=True)
    total = sum(k for k, _ in rows)
    out["total_gb"] = round(total / 1e6, 2)
    out["biggest"] = [{"path": p, "gb": round(k / 1e6, 2)} for k, p in rows[:limit]]
    return ToolResult(True, content=out, display=f"{base.name}: {out['total_gb']} GB")


# ================================================================== windows

def _screens():
    import AppKit
    main_h = AppKit.NSScreen.screens()[0].frame().size.height
    res = []
    for s in AppKit.NSScreen.screens():
        vf = s.visibleFrame()
        res.append({"x": vf.origin.x, "y": main_h - (vf.origin.y + vf.size.height),
                    "w": vf.size.width, "h": vf.size.height,
                    "name": str(s.localizedName())})
    return res


def tile_rect(screen: dict, where: str) -> tuple[float, float, float, float] | None:
    """Window rectangle (x, y, w, h), top-left origin, for a named placement."""
    x, y, w, h = screen["x"], screen["y"], screen["w"], screen["h"]
    half_w, half_h = w / 2, h / 2
    table = {
        "left": (x, y, half_w, h), "right": (x + half_w, y, half_w, h),
        "top": (x, y, w, half_h), "bottom": (x, y + half_h, w, half_h),
        "top_left": (x, y, half_w, half_h), "top_right": (x + half_w, y, half_w, half_h),
        "bottom_left": (x, y + half_h, half_w, half_h),
        "bottom_right": (x + half_w, y + half_h, half_w, half_h),
        "maximize": (x, y, w, h), "full": (x, y, w, h),
        "center": (x + w * 0.15, y + h * 0.1, w * 0.7, h * 0.8),
        "left_third": (x, y, w / 3, h), "middle_third": (x + w / 3, y, w / 3, h),
        "right_third": (x + 2 * w / 3, y, w / 3, h),
        "left_two_thirds": (x, y, 2 * w / 3, h), "right_two_thirds": (x + w / 3, y, 2 * w / 3, h),
    }
    return table.get(where.lower().replace(" ", "_").replace("-", "_"))


@REGISTRY.tool(
    "window_manage",
    "Arrange windows. action: list (every visible window with its position and "
    "size), move (x, y), resize (width, height), place (where: left | right | "
    "top | bottom | top_left | top_right | bottom_left | bottom_right | maximize "
    "| center | left_third | middle_third | right_third | left_two_thirds | "
    "right_two_thirds), minimize, fullscreen. Name the app; it acts on its "
    "front window. Needs the Accessibility permission for everything but list.",
    {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["list", "move", "resize", "place", "minimize", "fullscreen"]},
        "app": {"type": "string"},
        "where": {"type": "string"},
        "x": {"type": "integer"}, "y": {"type": "integer"},
        "width": {"type": "integer"}, "height": {"type": "integer"}},
     "required": ["action"]},
    risk=Risk.LOW, category="ui", parallel_safe=False)
def window_manage(action: str, app: str = "", where: str = "", x: int = 0, y: int = 0,
                  width: int = 0, height: int = 0):
    action = (action or "").lower()
    if action == "list":
        try:
            import Quartz
            wins = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID)
        except Exception as e:
            return ToolResult(False, error=f"cannot list windows: {e}")
        rows = []
        for w in wins or []:
            if w.get("kCGWindowLayer") != 0:
                continue
            b = w.get("kCGWindowBounds", {})
            if b.get("Width", 0) < 80 or b.get("Height", 0) < 60:
                continue
            owner = str(w.get("kCGWindowOwnerName", ""))
            if app and app.lower() not in owner.lower():
                continue
            rows.append({"app": owner, "title": str(w.get("kCGWindowName", "") or ""),
                         "x": int(b["X"]), "y": int(b["Y"]),
                         "width": int(b["Width"]), "height": int(b["Height"])})
        return ToolResult(True, content={"windows": rows[:40], "screens": _screens()},
                          display=f"{len(rows)} window(s)")

    if not app:
        return ToolResult(False, error="which app's window?")
    from . import ax
    if not ax.available():
        return ToolResult(False, error="Accessibility permission is needed to move windows; "
                                       "enable it for the app running JARVIS in System Settings > "
                                       "Privacy & Security > Accessibility")
    try:
        import ApplicationServices as AS
        import Quartz
        appel, _ = ax._app(app)
        win = ax._front_window(appel)
    except Exception as e:                        # noqa: BLE001
        return ToolResult(False, error=f"cannot reach {app}: {e}"[:200])
    if win is None:
        return ToolResult(False, error=f"{app} has no open window")

    def read():
        p, s = ax._attr(win, "AXPosition"), ax._attr(win, "AXSize")
        ok1, pt = AS.AXValueGetValue(p, AS.kAXValueCGPointType, None)
        ok2, sz = AS.AXValueGetValue(s, AS.kAXValueCGSizeType, None)
        return (int(pt.x), int(pt.y), int(sz.width), int(sz.height)) if ok1 and ok2 else None

    def put(px, py, w=None, h=None):
        AS.AXUIElementSetAttributeValue(win, "AXPosition", AS.AXValueCreate(
            AS.kAXValueCGPointType, Quartz.CGPoint(float(px), float(py))))
        if w is not None:
            AS.AXUIElementSetAttributeValue(win, "AXSize", AS.AXValueCreate(
                AS.kAXValueCGSizeType, Quartz.CGSize(float(w), float(h))))

    before = read()
    if action == "minimize":
        AS.AXUIElementSetAttributeValue(win, "AXMinimized", True)
        return ToolResult(True, content={"app": app, "minimized": True}, display=f"minimised {app}")
    if action == "fullscreen":
        AS.AXUIElementSetAttributeValue(win, "AXFullScreen", True)
        return ToolResult(True, content={"app": app, "fullscreen": True}, display=f"{app} full screen")
    if before is None:
        return ToolResult(False, error="could not read the window's position")
    if action == "move":
        put(x, y)
    elif action == "resize":
        if width < 100 or height < 80:
            return ToolResult(False, error="width and height must be at least 100 x 80")
        put(before[0], before[1], width, height)
    elif action == "place":
        cx, cy = before[0] + before[2] / 2, before[1] + before[3] / 2
        screens = _screens()
        scr = next((s for s in screens if s["x"] <= cx < s["x"] + s["w"] and
                    s["y"] <= cy < s["y"] + s["h"]), screens[0])
        rect = tile_rect(scr, where)
        if rect is None:
            return ToolResult(False, error=f"unknown placement {where!r}")
        put(*rect[:2], rect[2], rect[3])
    else:
        return ToolResult(False, error=f"unknown action {action!r}")
    time.sleep(0.15)
    after = read()
    changed = after is not None and after != before
    return ToolResult(changed, content={"app": app, "before": before, "after": after,
                                        "verified": changed},
                      error="" if changed else "the window did not move (some windows cannot be resized)",
                      display=f"{app}: {action} -> {after}")
