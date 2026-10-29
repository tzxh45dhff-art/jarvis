"""
find_anything: one search over everything the user has touched - files,
clipboard history, and what they read in the browser - ranked by meaning.

"The PDF about UPI", "that article about kiwi birds I read last week", "the
spreadsheet with attendance". Spotlight finds candidates by words in names
and contents (milliseconds, already indexed); browser history and clipboard
add what was read or copied; then the local sentence model ranks everything
by how well it *means* what was asked, with recent things nudged up. All on
this Mac.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from ..safety.policy import Risk
from .registry import REGISTRY, ToolResult

log = logging.getLogger("jarvis.find")

_SKIP_PATH = re.compile(r"/(Library|\.Trash|node_modules|\.venv|venv|\.git|__pycache__|"
                        r"site-packages|DerivedData|\.cache|Caches)/")
_STOP = {"the", "a", "an", "that", "this", "i", "my", "me", "about", "with", "on", "of", "for",
         "find", "where", "is", "was", "file", "document", "doc", "article", "page", "read",
         "saw", "opened", "last", "week", "yesterday", "today", "month", "morning", "some",
         "thing", "something", "one", "which", "had", "have", "there", "it", "in", "to", "from"}
_KIND_WORDS = {"pdf": ("pdf",), "spreadsheet": ("xlsx", "xls", "csv", "numbers"),
               "excel": ("xlsx", "xls"), "presentation": ("pptx", "key"), "slides": ("pptx", "key"),
               "deck": ("pptx", "key"), "photo": ("jpg", "jpeg", "png", "heic"),
               "image": ("jpg", "jpeg", "png", "heic", "webp"), "video": ("mp4", "mov", "mkv"),
               "word": ("docx", "doc", "pages"), "code": ("py", "js", "ts", "swift", "java", "cpp")}


def _when_window(q: str) -> tuple[float, float] | None:
    now = time.time()
    day = 86400
    low = q.lower()
    if "today" in low or "this morning" in low:
        start = dt.datetime.combine(dt.date.today(), dt.time()).timestamp()
        return start, now
    if "yesterday" in low:
        start = dt.datetime.combine(dt.date.today() - dt.timedelta(days=1), dt.time()).timestamp()
        return start, start + day
    if "last week" in low or "past week" in low:
        return now - 9 * day, now
    if "this week" in low:
        return now - 7 * day, now
    if "last month" in low:
        return now - 40 * day, now
    return None


def _words(q: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in _STOP and len(w) > 1]


def _spotlight(words: list[str], limit: int = 60) -> list[dict]:
    if not words:
        return []
    out: list[dict] = []
    seen = set()
    # All words first (precise), then any word (recall) if that was thin.
    queries = [" ".join(words)] + ([" | ".join(words)] if len(words) > 1 else [])
    for q in queries:
        try:
            r = subprocess.run(["mdfind", "-onlyin", str(Path.home()), q],
                               capture_output=True, text=True, timeout=6)
        except Exception:
            continue
        for p in r.stdout.splitlines():
            if p in seen or _SKIP_PATH.search(p) or "/." in p or os.path.basename(p).startswith("~$"):
                continue
            seen.add(p)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if os.path.isdir(p) and not p.endswith((".key", ".pages", ".numbers")):
                continue
            out.append({"source": "file", "path": p, "title": os.path.basename(p),
                        "where": str(Path(p).parent).replace(str(Path.home()), "~"),
                        "time": st.st_mtime})
            if len(out) >= limit:
                return out
        if len(out) >= 12:
            break
    return out


def _copy_db(path: Path) -> str | None:
    """Browsers keep their history database locked; read a copy."""
    try:
        tmp = tempfile.mkdtemp(prefix="jarvis-hist-")
        dst = os.path.join(tmp, "h.db")
        shutil.copy2(path, dst)
        for ext in ("-wal", "-shm"):
            if os.path.exists(str(path) + ext):
                shutil.copy2(str(path) + ext, dst + ext)
        return dst
    except (PermissionError, OSError):
        return None


def _browser(words: list[str], window, limit: int = 40) -> tuple[list[dict], list[str]]:
    out, notes = [], []
    like = [f"%{w}%" for w in words[:4]]
    if not like:
        return out, notes
    since = window[0] if window else time.time() - 120 * 86400
    # Safari: visit_time is seconds since 2001-01-01.
    safari = Path.home() / "Library/Safari/History.db"
    if safari.exists():
        db = _copy_db(safari)
        if db is None:
            notes.append("Safari history needs Full Disk Access for JARVIS Companion")
        else:
            try:
                c = sqlite3.connect(db)
                cond = " OR ".join(["(v.title LIKE ? OR i.url LIKE ?)"] * len(like))
                args = [x for l in like for x in (l, l)]
                rows = c.execute(
                    f"SELECT v.title, i.url, max(v.visit_time) FROM history_visits v "
                    f"JOIN history_items i ON i.id = v.history_item WHERE v.visit_time > ? "
                    f"AND ({cond}) GROUP BY i.url ORDER BY max(v.visit_time) DESC LIMIT ?",
                    [since - 978307200, *args, limit]).fetchall()
                for title, url, t in rows:
                    out.append({"source": "Safari", "title": title or url, "url": url,
                                "time": (t or 0) + 978307200})
            except sqlite3.Error as e:
                log.info("safari history: %s", e)
    # Chrome: last_visit_time is microseconds since 1601-01-01.
    chrome = Path.home() / "Library/Application Support/Google/Chrome/Default/History"
    if chrome.exists():
        db = _copy_db(chrome)
        if db:
            try:
                c = sqlite3.connect(db)
                cond = " OR ".join(["(title LIKE ? OR url LIKE ?)"] * len(like))
                args = [x for l in like for x in (l, l)]
                epoch = 11644473600
                rows = c.execute(
                    f"SELECT title, url, last_visit_time FROM urls WHERE last_visit_time > ? "
                    f"AND ({cond}) ORDER BY last_visit_time DESC LIMIT ?",
                    [int((since + epoch) * 1e6), *args, limit]).fetchall()
                for title, url, t in rows:
                    out.append({"source": "Chrome", "title": title or url, "url": url,
                                "time": t / 1e6 - epoch})
            except sqlite3.Error as e:
                log.info("chrome history: %s", e)
    return out, notes


def _rank(query: str, items: list[dict]) -> list[dict]:
    """By meaning (local sentence model), nudged by recency and word hits."""
    if not items:
        return items
    words = _words(query)
    try:
        from ..memory.store import get_store
        emb = get_store().embedder
        texts = [f"{i['title']} {i.get('where', '')} {i.get('url', '')[:80]}" for i in items]
        import numpy as np
        qv = np.asarray(emb.encode([query]))[0]
        mv = np.asarray(emb.encode(texts))
        qv = qv / (np.linalg.norm(qv) or 1)
        mv = mv / (np.linalg.norm(mv, axis=1, keepdims=True) + 1e-9)
        sims = mv @ qv
    except Exception as e:                                    # noqa: BLE001
        log.debug("semantic rank unavailable: %s", e)
        sims = [0.0] * len(items)
    now = time.time()
    for i, s in zip(items, sims):
        low = (i["title"] + " " + i.get("url", "") + " " + i.get("path", "")).lower()
        hits = sum(1 for w in words if w in low) / max(1, len(words))
        age_days = max(0.0, (now - (i.get("time") or 0)) / 86400)
        recency = 1 / (1 + age_days / 30)
        i["score"] = round(float(s) * 0.6 + hits * 0.3 + recency * 0.1, 3)
        i["_keep"] = float(s) >= 0.42 or hits >= 0.5
    # Better to say "nothing found" than to offer something unrelated that
    # merely contains one of the words somewhere inside it.
    kept = [i for i in items if i.pop("_keep")]
    return sorted(kept, key=lambda x: -x["score"])


@REGISTRY.tool(
    "find_anything",
    "Find something the user had, saw or read, described in their own words: a "
    "file ('the PDF about UPI', 'attendance spreadsheet'), a web page they read "
    "('that article about kiwi birds last week'), or something they copied. "
    "Searches files, browser history and clipboard history together, ranked by "
    "meaning. Use this when they don't know the exact name.",
    {"type": "object", "properties": {
        "query": {"type": "string", "description": "Their description, as they said it"},
        "kind": {"type": "string", "enum": ["any", "file", "web", "clipboard"]},
        "limit": {"type": "integer"}},
     "required": ["query"]},
    risk=Risk.SAFE, category="search", slow=True)
def find_anything(query: str, kind: str = "any", limit: int = 8):
    t0 = time.time()
    words = _words(query)
    window = _when_window(query)
    exts = next((v for k, v in _KIND_WORDS.items() if re.search(rf"\b{k}s?\b", query.lower())), None)
    items: list[dict] = []
    notes: list[str] = []
    if kind in ("any", "file"):
        files = _spotlight([w for w in words if not exts or w not in _KIND_WORDS])
        if exts:
            files = [f for f in files if f["path"].lower().rsplit(".", 1)[-1] in exts] or files
        items += files
    if kind in ("any", "web"):
        web, n = _browser(words, window)
        items += web
        notes += n
    if kind in ("any", "clipboard"):
        from ..memory import clips
        for c in clips.search(" ".join(words[:3]), limit=10):
            items.append({"source": "clipboard", "title": c["text"][:120], "time": time.time(),
                          "copied": c["copied"], "id": c["id"]})
    if window:
        items = [i for i in items if window[0] <= (i.get("time") or 0) <= window[1] + 60]
    ranked = _rank(query, items)[:max(1, min(limit, 20))]
    for i in ranked:
        i["when"] = time.strftime("%d %b %Y %H:%M", time.localtime(i.get("time") or 0))
        i.pop("time", None)
    content = {"results": ranked, "searched": f"{len(items)} candidate(s)",
               "seconds": round(time.time() - t0, 2)}
    if notes:
        content["note"] = "; ".join(notes)
    return ToolResult(True, content=content,
                      display=f"{len(ranked)} match(es) for {query!r}" if ranked else
                      f"nothing found for {query!r}")
