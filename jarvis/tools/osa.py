"""
AppleScript bridge.

User data is *never* interpolated into script source. Every value is passed as
an `argv` argument to an `on run argv` handler, so a contact name containing a
quote, a newline, or `" & do shell script "` is inert data rather than code.

macOS asks for Automation permission the first time each app is driven; the
prompt blocks `osascript`, so every call is bounded by a timeout and a refused
or unanswered prompt is reported as a clear, actionable error.
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger("jarvis.osa")

DEFAULT_TIMEOUT = 25
_PERMISSION_HINTS = (
    "not allowed", "not authorized", "assistive access", "-1743", "-1728 ",
    "Application isn't running", "-600",
)


@dataclass
class OSAResult:
    ok: bool
    out: str = ""
    error: str = ""

    @property
    def lines(self) -> list[str]:
        return [l for l in self.out.splitlines() if l.strip()]


def run(script: str, *args: str, timeout: int = DEFAULT_TIMEOUT,
        language: str = "AppleScript") -> OSAResult:
    """
    Execute `script`, which must define `on run argv ... end run`.

    Values reach the script through `argv` - they are arguments, not source.
    """
    cmd = ["osascript"]
    if language.lower() in ("javascript", "jxa"):
        cmd += ["-l", "JavaScript"]
    cmd += ["-e", script, "--"] + [str(a) for a in args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return OSAResult(False, error=(
            f"Timed out after {timeout}s. macOS is probably showing an "
            f"Automation permission dialog - approve it, then try again."))
    except Exception as e:
        return OSAResult(False, error=str(e))
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode != 0:
        low = err.lower()
        if any(h.lower() in low for h in _PERMISSION_HINTS):
            return OSAResult(False, error=(
                f"{err}  -> macOS blocked this. Approve it under System "
                f"Settings > Privacy & Security > Automation (and Accessibility "
                f"for UI control) for the app running JARVIS."))
        return OSAResult(False, error=err or f"osascript exited {p.returncode}")
    return OSAResult(True, out=out)


_PROC_CACHE = {"names": frozenset(), "at": 0.0}
_PROC_TTL = 1.5


def running_apps(force: bool = False) -> frozenset[str]:
    """
    Every running app name, in one call, cached for a moment.

    `tell application "System Events" to exists process "X"` takes about six
    seconds when X is *not* running - it appears to wait rather than answer.
    Listing every process instead costs ~0.2 s once and answers any number of
    membership questions, so nothing asks the slow question.
    """
    now = time.time()
    if not force and now - _PROC_CACHE["at"] < _PROC_TTL:
        return _PROC_CACHE["names"]
    # Launch services first: ~40 ms, no Automation permission. System Events
    # came back *empty* from the daemon's context, which made every
    # "is X running?" answer no - now_playing said "nothing is playing" while
    # Music was playing.
    names = frozenset()
    try:
        from .control import running_apps as _ls
        names = frozenset(a["name"] for a in _ls())
    except Exception:
        pass
    if not names:
        r = run('on run argv\n'
                'tell application "System Events" to return name of every '
                'application process whose background only is false\n'
                'end run', timeout=10)
        names = frozenset(a.strip() for a in r.out.split(",") if a.strip()) \
            if r.ok else frozenset()
    _PROC_CACHE.update(names=names, at=now)
    return names


def app_running(name: str) -> bool:
    return name in running_apps()


def app_installed(name: str) -> bool:
    from pathlib import Path
    for base in ("/Applications", "/System/Applications",
                 "/System/Applications/Utilities"):
        if (Path(base) / f"{name}.app").exists():
            return True
    return False


# Records are returned as delimiter-joined text: AppleScript lists of records
# are painful to serialise, and a rare unit separator cannot appear in real
# calendar or contact data.
FS = "\x1f"     # field separator
RS = "\x1e"     # record separator


def parse_records(out: str, fields: list[str]) -> list[dict]:
    rows = []
    for rec in out.split(RS):
        rec = rec.strip()
        if not rec:
            continue
        parts = rec.split(FS)
        parts += [""] * (len(fields) - len(parts))
        rows.append({f: parts[i].strip() for i, f in enumerate(fields)})
    return rows
