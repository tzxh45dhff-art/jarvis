"""Tools that let the model read and write long-term memory explicitly."""
from __future__ import annotations

from ..memory.store import KINDS, get_store
from ..safety.policy import Risk, Verdict
from .registry import REGISTRY, ToolResult


@REGISTRY.tool(
    "remember",
    "Store a durable fact about the user, their preferences, projects or decisions. "
    "Use when the user says 'remember...', states a lasting preference, or a project "
    "decision is made. Do not store transient chatter or anything secret.",
    {"type": "object", "properties": {
        "content": {"type": "string", "description": "The fact, written as a standalone sentence"},
        "kind": {"type": "string", "enum": list(KINDS),
                 "description": "semantic=stable fact, preference=how the user likes things, "
                                "project=ongoing work, episodic=something that happened"},
        "subject": {"type": "string", "description": "Short topic tag, e.g. 'editor'"}},
     "required": ["content"]},
    risk=Risk.LOW, category="write_new", parallel_safe=False)
def remember(content: str, kind: str = "semantic", subject: str = ""):
    mid, msg = get_store().add(content, kind=kind, subject=subject, source="explicit")
    if mid is None:
        return ToolResult(False, error=msg)
    return ToolResult(True, content={"id": mid, "status": msg},
                      display=f"remembered: {content[:70]}")


@REGISTRY.tool(
    "recall",
    "Search long-term memory for anything relevant to a topic or question.",
    {"type": "object", "properties": {
        "query": {"type": "string"},
        "kind": {"type": "string", "enum": list(KINDS)},
        "limit": {"type": "integer"}},
     "required": ["query"]},
    risk=Risk.SAFE, category="read")
def recall(query: str, kind: str = "", limit: int = 6):
    hits = get_store().search(query, k=limit, kinds=[kind] if kind else None)
    if not hits:
        return ToolResult(True, content={"memories": [], "note": "nothing relevant stored"},
                          display="recall: no matches")
    return ToolResult(True,
                      content={"memories": [{"id": m.id, "kind": m.kind,
                                             "content": m.content} for m in hits]},
                      display=f"recalled {len(hits)} memory(ies)")


@REGISTRY.tool(
    "list_memories",
    "List what is currently stored in long-term memory, newest first.",
    {"type": "object", "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "limit": {"type": "integer"}},
     "required": []},
    risk=Risk.SAFE, category="read")
def list_memories(kind: str = "", limit: int = 50):
    st = get_store()
    rows = st.all(kind=kind or None, limit=limit)
    return ToolResult(True, content={
        "counts": st.count(),
        "memories": [{"id": m.id, "kind": m.kind, "content": m.content}
                     for m in rows]},
        display=f"{len(rows)} memory(ies)")


@REGISTRY.tool(
    "forget",
    "Remove a stored memory. Pass the id from recall/list_memories, or a query to "
    "find the best match.",
    {"type": "object", "properties": {
        "id": {"type": "string"},
        "query": {"type": "string", "description": "Used if no id is given"}},
     "required": []},
    risk=Risk.MEDIUM, category="delete", parallel_safe=False,
    assess=lambda a: Verdict(Risk.LOW, "removes one stored memory (reversible in the DB)",
                             category="write_new"))
def forget(id: str = "", query: str = ""):
    st = get_store()
    mid = id
    if not mid and query:
        hits = st.search(query, k=1)
        if not hits:
            return ToolResult(False, error=f"No memory matches {query!r}")
        mid = hits[0].id
    if not mid:
        return ToolResult(False, error="Provide either an id or a query")
    ok = st.forget(mid)
    return ToolResult(ok, content={"forgotten": mid},
                      error="" if ok else f"No memory with id {mid}",
                      display=f"forgot memory {mid}")


@REGISTRY.tool(
    "set_default_app",
    "Set which app to use by default for a kind of task — music, browser, "
    "messaging, mail, editor, terminal or notes. Use whenever the user says "
    "things like 'use Spotify from now on' or 'open links in Chrome'. This is "
    "honoured exactly by the tools, unlike a remembered note.",
    {"type": "object", "properties": {
        "slot": {"type": "string",
                 "enum": ["music", "browser", "messaging", "mail", "editor",
                          "terminal", "notes"]},
        "app": {"type": "string",
                "description": "App name, e.g. Spotify, Google Chrome, WhatsApp"}},
     "required": ["slot", "app"]},
    risk=Risk.LOW, category="write_new", parallel_safe=False)
def set_default_app(slot: str, app: str):
    from ..memory.prefs import set_default
    ok, res = set_default(slot, app)
    if not ok:
        return ToolResult(False, error=res)
    return ToolResult(True, content={"slot": slot, "app": res},
                      display=f"default {slot} app is now {res}")


@REGISTRY.tool(
    "get_defaults",
    "Show which app is set as the default for each kind of task.",
    {"type": "object", "properties": {}, "required": []},
    risk=Risk.SAFE, category="read")
def get_defaults():
    from ..memory.prefs import SLOTS, all_defaults
    d = all_defaults()
    return ToolResult(True, content={"defaults": d,
                                     "unset": [s for s in SLOTS if s not in d]},
                      display=", ".join(f"{k}={v}" for k, v in d.items())
                              or "no defaults set")
