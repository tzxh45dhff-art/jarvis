"""
Default-app preferences.

Semantic memory is right for facts about a person, but wrong for a setting a
tool has to honour every single time: retrieval is fuzzy and probabilistic, so
"use Spotify from now on" would be found sometimes and missed other times. A
preference that is silently ignored half the time is worse than none.

These live in their own table, are exact-keyed, and are read directly by the
tools that need them — no embeddings, no ranking, no chance of a miss.
"""
from __future__ import annotations

import os
from pathlib import Path

import re
import sqlite3
import threading
import time

from ..config import DATA_DIR

DB = Path(os.environ.get("JARVIS_PREFS_DB") or DATA_DIR / "prefs.sqlite3")
_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prefs (
    slot    TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated REAL NOT NULL
);
"""

# The kinds of default that tools actually branch on.
SLOTS = {
    "music":    ("Music", "Spotify"),
    "browser":  ("Safari", "Google Chrome", "Brave Browser", "Arc",
                 "Microsoft Edge", "Firefox"),
    "messaging": ("Messages", "WhatsApp"),
    "mail":     ("Mail", "Gmail", "Outlook"),
    "editor":   (),      # free-form: any installed app
    "terminal": (),
    "notes":    (),
}

# Spoken names people actually use, mapped to the real app.
_ALIASES = {
    "apple music": "Music", "itunes": "Music", "music.app": "Music",
    "spotify": "Spotify",
    "chrome": "Google Chrome", "google chrome": "Google Chrome",
    "brave": "Brave Browser", "arc": "Arc", "edge": "Microsoft Edge",
    "safari": "Safari", "firefox": "Firefox",
    "imessage": "Messages", "messages": "Messages", "whatsapp": "WhatsApp",
    "mail": "Mail", "apple mail": "Mail", "outlook": "Outlook",
    "vscode": "Visual Studio Code", "vs code": "Visual Studio Code",
    "code": "Visual Studio Code", "antigravity": "Antigravity IDE",
    "terminal": "Terminal", "iterm": "iTerm", "iterm2": "iTerm",
    "notes": "Notes", "obsidian": "Obsidian",
}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=10, check_same_thread=False)
    c.executescript(_SCHEMA)
    return c


def normalise_app(name: str) -> str:
    n = (name or "").strip()
    return _ALIASES.get(n.lower(), n)


def set_default(slot: str, app: str) -> tuple[bool, str]:
    slot = (slot or "").strip().lower()
    if slot not in SLOTS:
        return False, (f"Unknown preference {slot!r}. Known: "
                       f"{', '.join(sorted(SLOTS))}")
    app = normalise_app(app)
    allowed = SLOTS[slot]
    if allowed and app not in allowed:
        return False, (f"{app!r} is not a valid {slot} app. Choose from: "
                       f"{', '.join(allowed)}")
    with _LOCK, _conn() as c:
        c.execute("INSERT INTO prefs(slot,value,updated) VALUES(?,?,?) "
                  "ON CONFLICT(slot) DO UPDATE SET value=excluded.value, "
                  "updated=excluded.updated", (slot, app, time.time()))
    return True, app


def get_default(slot: str, fallback: str = "") -> str:
    try:
        with _conn() as c:
            row = c.execute("SELECT value FROM prefs WHERE slot=?",
                            (slot,)).fetchone()
        return row[0] if row else fallback
    except Exception:
        return fallback


def clear_default(slot: str) -> bool:
    with _LOCK, _conn() as c:
        return c.execute("DELETE FROM prefs WHERE slot=?", (slot,)).rowcount > 0


def all_defaults() -> dict[str, str]:
    try:
        with _conn() as c:
            return {r[0]: r[1] for r in
                    c.execute("SELECT slot,value FROM prefs").fetchall()}
    except Exception:
        return {}


# Phrases like "use Spotify as my default music app" / "always open links in Chrome"
_SET_RE = re.compile(
    r"\b(?:use|using|make|set|switch to|default to|always|prefer|open .{0,12}? (?:in|with)|from now on)\b.{0,26}?"
    r"\b(" + "|".join(sorted((k for k in _ALIASES), key=len, reverse=True)) + r")\b",
    re.I)
_SLOT_HINT = {
    "music": ("music", "song", "play", "track", "album", "listen"),
    "browser": ("browser", "web", "link", "site", "search online", "internet"),
    "messaging": ("message", "text", "chat", "dm"),
    "mail": ("mail", "email"),
    "editor": ("editor", "ide", "code"),
    "terminal": ("terminal", "shell", "console"),
    "notes": ("note", "notes"),
}


def infer_default_from(text: str) -> tuple[str, str] | None:
    """Spot 'use X as my default Y' in ordinary speech. Returns (slot, app)."""
    m = _SET_RE.search(text or "")
    if not m:
        return None
    app = normalise_app(m.group(1))
    low = (text or "").lower()
    for slot, words in _SLOT_HINT.items():
        if any(w in low for w in words):
            allowed = SLOTS.get(slot) or ()
            if not allowed or app in allowed:
                return slot, app
    for slot, allowed in SLOTS.items():
        if allowed and app in allowed:
            return slot, app
    return None
