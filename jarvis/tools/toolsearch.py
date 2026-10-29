"""
Tool search: keeps the always-on toolset small while the whole toolbox grows.

Providers refuse a request that offers more than 128 tools (Azure answers
"Invalid 'tools': array too long"), and a model chooses better from a short
list. Rarely needed tools are therefore *deferred*: listed by name in the
prompt, loaded into the request only after `find_tools` is called for them.
Once found they stay available for the rest of the conversation.

This module is imported last so every tool exists before it is deferred.
"""
from __future__ import annotations

import re

from ..safety.policy import Risk
from .registry import REGISTRY, ToolResult

# Specialised or occasional tools. Everything people ask for every day (files,
# web, apps, screen, music, calendar, messages, presentations) stays loaded.
DEFERRED = [
    # media editing
    "image_edit", "image_combine", "video_tool", "transcribe_media",
    "blender_script", "presentation_preview", "archive_tool", "learning_settings", "forget_about_me", "list_routines", "screen_info", "set_personality", "search_music", "inspect_ui", "file_dialog", "drag",
    # machine administration
    "network_info", "disk_usage", "window_manage",
    # online extras
    "http_request", "schedule_task", "find_images",
    # lookups
    # long tail of older tools
    "list_installed_apps", "get_defaults", "set_default_app", "list_shortcuts",
    "search_papers", "leetcode_next", "set_wifi", "mouse_move", "list_memories",
    "forget", "set_dark_mode", "lock_screen",
]

# Plain-language synonyms, so "run this every morning" finds schedule_task and
# "what is hogging my cpu" finds processes without the model guessing a name.
HINTS = {
    "schedule_task": "schedule recurring repeat daily weekly every morning evening night "
                     "automatically later unattended briefing automate",
    "window_manage": "window tile snap arrange left right half maximize minimise "
                     "position resize screen layout side by side",
    "processes": "cpu memory ram hogging slow running programs quit kill force stuck frozen "
                 "activity monitor busy",
    "disk_usage": "disk space storage full big large files folder gigabytes clean cleanup free",
    "network_info": "wifi internet connection speed test ping slow down offline ip dns "
                    "bandwidth website reachable",
    "youtube_tool": "youtube video transcript captions summarise summarize watch channel",
    "video_tool": "video clip trim cut join merge speed slow motion vertical crop compress "
                  "gif mute fade audio edit footage reel",
    "transcribe_media": "transcribe transcript speech recording meeting lecture interview "
                        "subtitles voice note audio text",
    "image_edit": "image photo picture resize crop rotate filter brightness text caption "
                  "watermark border grayscale edit",
    "image_combine": "image photos collage grid montage combine side by side contact sheet",
    "make_chart": "chart graph plot bar line pie visualise visualize data figure",
    "blender_script": "blender 3d model render scene mesh animation bpy",
    "learning_settings": "stop learning about me privacy turn off learning watching tracking pause memory",
    "drag": "drag and drop move by dragging slider drag file onto",
    "file_dialog": "save dialog open dialog choose file panel browse for file upload picker",
    "inspect_ui": "inspect ui accessibility tree debug controls element hierarchy",
    "search_music": "search my music library songs albums artists in apple music library",
    "set_personality": "be more formal casual shorter answers longer detailed talk in hinglish english funny humour personality tone style of talking",
    "list_routines": "my routines list routines delete routine show routines what routines",
    "screen_info": "display resolution screens monitors screen size retina",
    "forget_about_me": "forget about me delete what you know erase my profile forget that i like remove learned",
    "http_request": "api http request endpoint json rest post get webhook curl",
    "directions": "directions route drive walk distance travel time commute maps navigate",
    "archive_tool": "zip unzip compress archive extract tar bundle",
    "presentation_preview": "preview slide deck picture of presentation",
    "define_word": "define definition meaning dictionary word synonym",
    "find_images": "photo picture image stock free licensed illustration for document slide",
    "time_in": "time zone clock city timezone what time",
}

_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("the and for with from this that your are use any you when not can will "
            "its into each per them they have has what how which about than then "
            "also but out over only some more most make made get set run".split())


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if len(w) > 2 and w not in _STOP}


def search(query: str, limit: int = 6) -> list[tuple[float, str]]:
    q = _tokens(query)
    if not q:
        return []
    scored = []
    for t in REGISTRY._tools.values():
        if t.name == "find_tools":
            continue
        name_toks = _tokens(t.name.replace("_", " "))
        desc_toks = _tokens(t.description)
        hint_toks = _tokens(HINTS.get(t.name, ""))
        score = 3.0 * len(q & name_toks) + 1.0 * len(q & desc_toks) + 2.0 * len(q & hint_toks)
        # a substring of the name counts: "chart" finds make_chart
        score += sum(2.0 for w in q if w in t.name)
        if score > 0:
            scored.append((score + (1.5 if t.deferred else 0.0), t.name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    if not scored:
        return []
    top = scored[0][0]
    # drop the long tail of weak, incidental matches
    return [(sc, n) for sc, n in scored if sc >= max(2.0, 0.45 * top)][:limit]


@REGISTRY.tool(
    "find_tools",
    "Load more tools. Some tools are not available until you ask for them "
    "(they are listed under 'More tools' in your instructions). Call this with "
    "a few words about what you need - 'chart', 'video trim', 'youtube "
    "transcript', 'move window', 'running programs' - and the matching tools "
    "become available on your next step.",
    {"type": "object", "properties": {
        "query": {"type": "string", "description": "What you need to do"}},
     "required": ["query"]},
    risk=Risk.SAFE, category="read", parallel_safe=True)
def find_tools(query: str):
    hits = search(query)
    if not hits:
        names = [n for n, _ in REGISTRY.deferred_catalog()]
        return ToolResult(False, error="nothing matched; the extra tools are: " + ", ".join(names))
    enabled = REGISTRY.enable([n for _, n in hits])
    out = []
    for n in enabled:
        t = REGISTRY.get(n)
        props = (t.parameters or {}).get("properties", {})
        out.append({"name": n, "use_for": t.description.split(". ")[0][:160],
                    "parameters": {k: (v.get("type", "any") + (f" ({v['description'][:60]})"
                                                              if v.get("description") else ""))
                                   for k, v in list(props.items())[:10]}})
    return ToolResult(True, content={
        "now_available": out,
        "note": "these tools are loaded; call the one you need directly"},
        display=f"found {len(out)} tool(s): " + ", ".join(enabled[:4]))


# Requests that name a deferred tool's job outright. Loading the tool before the
# first step saves the find_tools round trip - and that round trip also changed
# the tool list mid-turn, which re-stores the whole prompt cache (measured: a
# Blender job paid Rs 20 on Astra for it).
PRELOAD = [
    (re.compile(r"\bblender\b", re.I), ["blender_script"]),
    (re.compile(r"\b(trim|cut|merge|join|speed up|slow down|compress|crop|mute|gif)\b.{0,40}"
                r"\b(video|clip|footage|reel|mp4|mov)\b|\b(video|clip|footage|reel|mp4|mov)\b.{0,40}"
                r"\b(trim|cut|merge|join|speed|slow|compress|crop|mute|gif)\b", re.I), ["video_tool"]),
    (re.compile(r"\btranscri(be|pt|ption)\b", re.I), ["transcribe_media"]),
    (re.compile(r"\b(zip|unzip|archive|extract|\.tar|tarball)\b", re.I), ["archive_tool"]),
    (re.compile(r"\b(crop|resize|rotate|watermark|grayscale|greyscale)\b.{0,40}\b(image|photo|picture|png|jpe?g)\b"
                r"|\b(image|photo|picture|png|jpe?g)\b.{0,40}\b(crop|resize|rotate|watermark|grayscale|greyscale)\b",
                re.I), ["image_edit"]),
    (re.compile(r"\bcollage\b|\bcontact sheet\b", re.I), ["image_combine"]),
    (re.compile(r"\bleetcode\b", re.I), ["leetcode_next"]),
]


def preload_for(text: str) -> list[str]:
    """Switch on the deferred tools this request plainly needs."""
    want = [n for rx, names in PRELOAD if rx.search(text or "") for n in names]
    return REGISTRY.enable(want) if want else []


REGISTRY.defer(DEFERRED)
