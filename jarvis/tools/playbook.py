"""
Site and app know-how, kept as plain Markdown the agent reads before acting.

General tools get a model most of the way; the last mile on a particular site
is facts - which endpoint lists the user's progress, which button submits, what
"done" looks like there. Those live in jarvis/playbooks/*.md, one file per site
or app, so adding one is writing a page rather than changing code.
"""
from __future__ import annotations

from pathlib import Path

from ..safety.policy import Risk
from .registry import REGISTRY, ToolResult

PLAYBOOK_DIR = Path(__file__).resolve().parent.parent / "playbooks"


def available() -> list[str]:
    return sorted(p.stem for p in PLAYBOOK_DIR.glob("*.md"))


_ALIASES = {"powerpoint": "keynote", "ppt": "keynote", "slides": "keynote",
            "presentation": "keynote", "pages": "word", "numbers": "excel",
            "spreadsheet": "excel", "resolve": "davinci", "premiere": "davinci",
            "final cut": "imovie", "logic": "garageband", "ableton": "garageband",
            "sketch": "figma",
            "netflix": "streaming", "youtube": "streaming", "prime video": "streaming",
            "hotstar": "streaming", "disney": "streaming", "jiocinema": "streaming",
            "spotify": "streaming", "twitch": "streaming", "vimeo": "streaming"}


def for_request(text: str) -> tuple[str, str] | None:
    """(name, text) of the playbook a request is about, when it names the
    site or app outright - so it rides along with the request instead of
    costing the model a whole round trip (~4 s) to ask for it."""
    import re
    low = (text or "").lower()
    names = available()
    for n in names:
        if re.search(rf"\b{re.escape(n)}\b", low):
            return n, (PLAYBOOK_DIR / f"{n}.md").read_text()
    for needle, target in _ALIASES.items():
        # music is handled by play_music; the rest are ordinary words too
        if needle in ("spotify", "resolve", "logic", "sketch", "pages", "numbers", "slides"):
            continue
        if target in names and re.search(rf"\b{re.escape(needle)}\b", low):
            return target, (PLAYBOOK_DIR / f"{target}.md").read_text()
    return None


@REGISTRY.tool(
    "playbook",
    "Step-by-step know-how for multi-step jobs on a specific website or app: "
    "what 'done' means there, the reliable way through it, and what to do when "
    "blocked. Call it before starting such a job. Available: "
    + (", ".join(available()) or "none yet") + ". Call with no name to list.",
    {"type": "object", "properties": {
        "name": {"type": "string", "description": "Site or app, e.g. 'leetcode'"}},
     "required": []},
    risk=Risk.SAFE, category="read")
def playbook(name: str = ""):
    names = available()
    if not name:
        return ToolResult(True, content={"playbooks": names},
                          display=f"{len(names)} playbook(s)")
    q = name.lower().replace("www.", "").split(".")[0].strip()
    hit = next((n for n in names if n == q), None) or \
        next((n for n in names if n in q or q in n), None)
    if hit is None:
        # an app known by another name, or sharing a playbook with its sibling
        for needle, target in _ALIASES.items():
            if needle in q and target in names:
                hit = target
                break
    if hit is None:
        return ToolResult(True, content={"playbook": None, "available": names},
                          display=f"no playbook for {name!r}")
    text = (PLAYBOOK_DIR / f"{hit}.md").read_text()
    return ToolResult(True, content={"playbook": hit, "steps": text},
                      display=f"read the {hit} playbook")


# ------------------------------------------------------------------ LeetCode
#
# "Solve the next question" needs to know what the user already solved. The
# in-page route needs Safari's "Allow JavaScript from Apple Events"; this one
# needs nothing: LeetCode's public GraphQL lists a user's recent accepted
# solutions, and its public problem list has every problem's difficulty.

import json as _json
import time as _time
import urllib.request as _ur

from ..config import DATA_DIR

_LC_USER = DATA_DIR / "leetcode.json"
_LC_PROBLEMS: dict = {"at": 0.0, "rows": []}
_LC_HEADERS = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json",
               "Referer": "https://leetcode.com"}


def _lc_problems() -> list[dict]:
    if _time.time() - _LC_PROBLEMS["at"] < 6 * 3600 and _LC_PROBLEMS["rows"]:
        return _LC_PROBLEMS["rows"]
    req = _ur.Request("https://leetcode.com/api/problems/all/", headers=_LC_HEADERS)
    with _ur.urlopen(req, timeout=30) as r:
        d = _json.load(r)
    rows = [{"id": p["stat"]["frontend_question_id"],
             "title": p["stat"]["question__title"],
             "slug": p["stat"]["question__title_slug"],
             "difficulty": {1: "Easy", 2: "Medium", 3: "Hard"}.get(
                 p["difficulty"]["level"], "?"),
             "paid": p["paid_only"]} for p in d["stat_status_pairs"]]
    rows.sort(key=lambda r: r["id"])
    _LC_PROBLEMS.update(at=_time.time(), rows=rows)
    return rows


def _lc_solved(user: str) -> tuple[set[str], int | None]:
    q = {"query": "query($u:String!){matchedUser(username:$u){submitStatsGlobal"
                  "{acSubmissionNum{difficulty count}}} recentAcSubmissionList"
                  "(username:$u,limit:50){titleSlug}}", "variables": {"u": user}}
    req = _ur.Request("https://leetcode.com/graphql", data=_json.dumps(q).encode(),
                      headers=_LC_HEADERS)
    with _ur.urlopen(req, timeout=30) as r:
        d = _json.load(r)
    data = d.get("data") or {}
    if not data.get("matchedUser"):
        raise LookupError(f"LeetCode has no user called {user!r}")
    total = next((x["count"] for x in data["matchedUser"]["submitStatsGlobal"]
                  ["acSubmissionNum"] if x["difficulty"] == "All"), None)
    return {x["titleSlug"] for x in data.get("recentAcSubmissionList") or []}, total


@REGISTRY.tool(
    "leetcode_next",
    "Find the user's next unsolved LeetCode problem - the lowest-numbered free "
    "one they have not solved, optionally of a given difficulty - from "
    "LeetCode's public data (works without any browser setting). Needs their "
    "LeetCode username once; it is remembered after that. If you don't know "
    "it, ask the user.",
    {"type": "object", "properties": {
        "username": {"type": "string", "description": "LeetCode username, if not saved yet"},
        "difficulty": {"type": "string", "enum": ["Easy", "Medium", "Hard", "Any"]},
        "count": {"type": "integer", "description": "How many to suggest, default 1"}},
     "required": []},
    risk=Risk.SAFE, category="search")
def leetcode_next(username: str = "", difficulty: str = "Any", count: int = 1):
    user = username.strip()
    if not user:
        try:
            user = _json.loads(_LC_USER.read_text()).get("username", "")
        except Exception:
            user = ""
    if not user:
        return ToolResult(False, error="I don't know the user's LeetCode username - "
                                       "ask them for it, then call leetcode_next "
                                       "with username=...")
    try:
        solved, total = _lc_solved(user)
        problems = _lc_problems()
    except LookupError as e:
        return ToolResult(False, error=str(e) + " - check the spelling with the user")
    except Exception as e:
        return ToolResult(False, error=f"could not reach LeetCode: {e}")
    if username.strip():
        _LC_USER.write_text(_json.dumps({"username": user}))
    pool = [p for p in problems if not p["paid"] and p["slug"] not in solved
            and (difficulty in ("Any", "") or p["difficulty"] == difficulty)]
    picks = pool[:max(1, min(int(count or 1), 10))]
    for p in picks:
        p["url"] = f"https://leetcode.com/problems/{p['slug']}/description/"
    content = {"username": user, "solved_total": total, "next": picks}
    if total is not None and total > len(solved):
        content["note"] = (f"LeetCode only lists the {len(solved)} most recent "
                           f"solutions of {total}; a suggestion may already be "
                           f"solved - check the page shows it unsolved")
    first = picks[0] if picks else None
    return ToolResult(True, content=content,
                      display=(f"next unsolved: {first['id']}. {first['title']} "
                               f"({first['difficulty']})" if first else
                               "no unsolved problems match"))


# ------------------------------------------------------------------ coach mode

def coach_on() -> bool:
    from ..memory.profile import _setting
    return _setting("leetcode_coach", "off") == "on"


def _lc_slug_on_screen() -> tuple[str, str]:
    """(slug, title) of the LeetCode problem in front, from the window title or
    the browser's URL."""
    from ..context import front_window
    app, title = front_window()
    slug = ""
    try:
        from . import ax
        from .browser import _active_browser
        br = _active_browser() or app
        appel, _ = ax._app(br)
        area = ax._web_area(appel)
        url = str(ax._attr(area, "AXURL") or "") if area is not None else ""
        import re as _re
        m = _re.search(r"leetcode\.com/problems/([a-z0-9-]+)", url)
        slug = m.group(1) if m else ""
    except Exception:
        pass
    if not slug and "leetcode" in (title or "").lower():
        name = title.split(" - LeetCode")[0].strip()
        import re as _re
        slug = _re.sub(r"[^a-z0-9]+", "-", _re.sub(r"^\d+\.\s*", "", name).lower()).strip("-")
    return slug, title


@REGISTRY.tool(
    "leetcode_coach",
    "LeetCode coach: turn coach mode on/off (hints instead of solutions), give "
    "the next hint for the problem on screen, or fetch the user's code to review.",
    {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["on", "off", "hint", "review"]},
        "problem": {"type": "string", "description": "Slug, if not the one on screen"}},
     "required": ["action"]},
    risk=Risk.SAFE, category="read")
def leetcode_coach(action: str, problem: str = ""):
    from ..memory.profile import _setting, set_setting
    if action in ("on", "off"):
        set_setting("leetcode_coach", action)
        return ToolResult(True, content={"coach": action, "verified": True},
                          display=f"LeetCode coach {action}")
    slug, title = (problem, "") if problem else _lc_slug_on_screen()
    if not slug:
        return ToolResult(False, error="no LeetCode problem is open in the browser; "
                                       "ask which problem, or open it first")
    text = ""
    try:
        from .ax import web_text
        from ..context import front_window
        app = front_window()[0]
        text = web_text(app, max_chars=9000) or ""
    except Exception:
        pass
    if action == "hint":
        key = f"lc_hint:{slug}"
        level = min(3, int(_setting(key, "0") or 0) + 1)
        set_setting(key, str(level))
        return ToolResult(True, content={"problem": slug, "hint_level": level,
                                         "page_text": text[:6000],
                                         "rule": "give exactly one hint at this level; no code"},
                          display=f"hint {level} for {slug}")
    return ToolResult(True, content={"problem": slug, "page_text": text[:9000],
                                     "rule": "review: bugs, edge cases, complexity; "
                                             "pointers not a rewrite"},
                      display=f"reviewing your code for {slug}")
