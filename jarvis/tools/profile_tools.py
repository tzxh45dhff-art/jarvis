"""
The user's view of, and control over, what JARVIS has learned about them.
"""
from __future__ import annotations

from ..memory import profile
from ..safety.policy import Risk, Verdict
from .registry import REGISTRY, ToolResult


@REGISTRY.tool(
    "about_me",
    "What you have learned about the user: tastes (music, shows, platforms), "
    "people, routines, preferences. Use for 'what do you know about me', 'what "
    "music do I like', or before choosing something on their behalf.",
    {"type": "object", "properties": {
        "category": {"type": "string", "enum": list(profile.CATEGORIES),
                     "description": "Only this area (optional)"}},
     "required": []},
    risk=Risk.SAFE, category="read")
def about_me(category: str = ""):
    rows = profile.facts(category or None, min_conf=0.45)
    if not rows:
        return ToolResult(True, content={"facts": [], "learning": profile.learning_enabled()},
                          display="nothing learned yet")
    out = [{"category": r["category"], "item": r["item"],
            "feeling": {1: "likes", 0: "fact", -1: "dislikes"}[r["polarity"]],
            "detail": r["detail"], "how": {"said": "you said so", "did": "from what you do",
                                           "told": "you asked me to remember"}.get(r["source"], r["source"]),
            "sure": round(r["confidence"], 2)} for r in rows[:60]]
    return ToolResult(True, content={"facts": out, "learning": profile.learning_enabled(),
                                     "watching": profile.watching_enabled()},
                      display=f"{len(rows)} thing(s) known about you")


@REGISTRY.tool(
    "remember_about_me",
    "Save a lasting fact about the user that they told you directly (a taste, a "
    "dislike, a person, a routine, how they like answers).",
    {"type": "object", "properties": {
        "category": {"type": "string", "enum": list(profile.CATEGORIES)},
        "item": {"type": "string", "description": "e.g. 'Arctic Monkeys', 'Mom = Kiran Kaur'"},
        "feeling": {"type": "string", "enum": ["likes", "fact", "dislikes"]},
        "detail": {"type": "string"}},
     "required": ["category", "item"]},
    risk=Risk.LOW, category="write_new")
def remember_about_me(category: str, item: str, feeling: str = "likes", detail: str = ""):
    pol = {"likes": 1, "fact": 0, "dislikes": -1}.get(feeling, 1)
    ok = profile.upsert(category, item, pol, detail, confidence=0.95, source="told")
    profile.invalidate()
    if not ok:
        return ToolResult(False, error="not saved: empty, too long, or sensitive")
    return ToolResult(True, content={"saved": True, "verified": True},
                      display=f"noted: {feeling} {item}")


def _forget_verdict(args: dict) -> Verdict:
    if args.get("everything"):
        return Verdict(Risk.HIGH, "erases everything JARVIS has learned about you",
                       category="delete", needs_confirm=True)
    return Verdict(Risk.LOW, "forgets matching facts", category="write_new")


@REGISTRY.tool(
    "forget_about_me",
    "Forget learned facts about the user that match a word ('Coldplay', 'music'), "
    "or everything (asks first).",
    {"type": "object", "properties": {
        "matching": {"type": "string"},
        "everything": {"type": "boolean"}},
     "required": []},
    risk=Risk.LOW, category="write_new", assess=_forget_verdict)
def forget_about_me(matching: str = "", everything: bool = False):
    if not everything and not matching.strip():
        return ToolResult(False, error="say what to forget, or everything=true")
    n = profile.forget(matching, everything=everything)
    profile.invalidate()
    return ToolResult(True, content={"forgotten": n, "verified": True},
                      display=f"forgot {n} item(s)")


@REGISTRY.tool(
    "learning_settings",
    "Turn learning about the user on or off: 'learning' (everything) or 'watching' "
    "(noticing what plays and which app is in front).",
    {"type": "object", "properties": {
        "learning": {"type": "string", "enum": ["on", "off"]},
        "watching": {"type": "string", "enum": ["on", "off"]}},
     "required": []},
    risk=Risk.LOW, category="system_pref")
def learning_settings(learning: str = "", watching: str = ""):
    if learning:
        profile.set_setting("learning", learning)
    if watching:
        profile.set_setting("watching", watching)
    return ToolResult(True, content={"learning": profile.learning_enabled(),
                                     "watching": profile.watching_enabled(), "verified": True},
                      display=f"learning {'on' if profile.learning_enabled() else 'off'}, "
                              f"watching {'on' if profile.watching_enabled() else 'off'}")


@REGISTRY.tool(
    "save_routine",
    "Save a routine the user can start with one phrase, e.g. 'study mode' = "
    "['open Notion', 'open Safari', 'play lo-fi on Spotify', 'turn on Do Not "
    "Disturb']. Steps are plain requests, run in order when they say the name.",
    {"type": "object", "properties": {
        "name": {"type": "string", "description": "The phrase, e.g. 'study mode'"},
        "steps": {"type": "array", "items": {"type": "string"}}},
     "required": ["name", "steps"]},
    risk=Risk.LOW, category="write_new")
def save_routine(name: str, steps: list):
    if not profile.save_routine(name, steps):
        return ToolResult(False, error="a routine needs a name and at least one step")
    return ToolResult(True, content={"saved": name.lower(), "steps": steps, "verified": True},
                      display=f"saved routine '{name}' ({len(steps)} steps)")


@REGISTRY.tool(
    "list_routines",
    "List the user's saved routines, or delete one.",
    {"type": "object", "properties": {"delete": {"type": "string"}}, "required": []},
    risk=Risk.LOW, category="read")
def list_routines(delete: str = ""):
    if delete:
        ok = profile.delete_routine(delete)
        return ToolResult(ok, content={"deleted": delete} if ok else None,
                          error="" if ok else f"no routine called {delete!r}",
                          display=f"deleted routine '{delete}'" if ok else "")
    r = profile.routines()
    return ToolResult(True, content={"routines": r}, display=f"{len(r)} routine(s)")


@REGISTRY.tool(
    "set_personality",
    "Change how you talk to the user: answer length, tone, language and humour. "
    "Use when they say 'be more formal', 'shorter answers', 'talk in Hinglish', "
    "'be funnier' and the like. Only pass what they asked to change.",
    {"type": "object", "properties": {
        "length": {"type": "string", "enum": ["short", "balanced", "detailed"]},
        "tone": {"type": "string", "enum": ["casual", "friendly", "formal"]},
        "language": {"type": "string", "enum": ["auto", "english", "hinglish"]},
        "humour": {"type": "string", "enum": ["on", "off"]}},
     "required": []},
    risk=Risk.LOW, category="system_pref")
def set_personality(length: str = "", tone: str = "", language: str = "", humour: str = ""):
    kw = {k: v for k, v in (("length", length), ("tone", tone), ("language", language),
                            ("humour", humour)) if v}
    now = profile.set_personality(**kw)
    return ToolResult(True, content={"personality": now, "verified": True},
                      display=", ".join(f"{k} {v}" for k, v in now.items()))


@REGISTRY.tool(
    "important_dates",
    "Birthdays, anniversaries and other dates that matter to the user - theirs and "
    "the people in their life. action='save' when they tell you one ('Mom's "
    "birthday is 12 March', person='me' for their own); 'upcoming' for what's "
    "coming up; 'find' to look one up ('when is Arjun's birthday').",
    {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["save", "upcoming", "find"]},
        "person": {"type": "string", "description": "Name as they say it, or 'me'"},
        "label": {"type": "string", "description": "birthday, anniversary, exam..."},
        "date": {"type": "string", "description": "e.g. '12 March', '12 March 1975'"},
        "days": {"type": "integer", "description": "For upcoming: how far ahead (default 30)"}},
     "required": ["action"]},
    risk=Risk.LOW, category="read")
def important_dates(action: str, person: str = "", label: str = "birthday", date: str = "",
                    days: int = 30):
    if action == "save":
        when = profile.parse_day(date)
        if not person or not when:
            return ToolResult(False, error="need who and a date like '12 March'")
        if not profile.save_date(person, label or "birthday", *when, source="told"):
            return ToolResult(False, error=f"{date!r} is not a real date")
        return ToolResult(True, content={"saved": {"person": person, "label": label, "date": date},
                                         "verified": True},
                          display=f"saved {'your' if person.lower() in ('me', 'my', 'i') else person + "'s"} {label}: {date}")
    if action == "find":
        rows = profile.dates(person)
        return ToolResult(True, content={"dates": rows}, display=f"{len(rows)} date(s)")
    soon = profile.upcoming(max(1, min(days, 366)))
    return ToolResult(True, content={"upcoming": [dict(d, said=profile.describe_date(d)) for d in soon]},
                      display=f"{len(soon)} coming up")


@REGISTRY.tool(
    "replace_selection",
    "Replace the text the user has selected (in the app in front) with new text: "
    "after rewriting, translating, fixing or shortening what they selected. Give "
    "only the new text, nothing around it.",
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    risk=Risk.LOW, category="write_new", parallel_safe=False)
def replace_selection(text: str):
    from .. import selection
    ok, how = selection.replace(text)
    if not ok:
        return ToolResult(False, error=f"could not put the text in place ({how}); "
                                       f"give it to the user to paste instead")
    return ToolResult(True, content={"replaced": True, "how": how, "chars": len(text),
                                     "note": "the user can press Cmd+Z to undo"},
                      display=f"replaced the selection ({how})")
